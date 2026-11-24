# -*- coding: utf-8 -*-
"""
backend.py —— B 电脑上的模型后端。

职责：
  1. 复用 B 电脑上已安装的 ComfyUI 代码库（comfy.sd / folder_paths），
     这样 CLIP / VAE 的加载与编码方式和 ComfyUI 官方节点完全一致（不会出现
     "独立实现导致 conditioning 数值对不上本机 UNet" 的坑）。
  2. 惰性加载 + LRU 淘汰（默认各只保留 1 个），显存/内存不会被模型堆满。
  3. 提供 3 个原子操作：clip_encode / vae_decode / vae_encode。

导入 comfy 前必须清空 sys.argv：comfy.cli_args 在 import 时就 parse_args()，
我们自己的命令行参数会让它报错 —— 这是很多人搭独立服务端时踩的坑。
"""

from __future__ import annotations

import gc
import hashlib
import importlib
import json
import math
import os
import subprocess
import sys
import threading
import time
import traceback
import types
from collections import OrderedDict

import torch

import codec
from llm import LLMEngine

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 扩散模型 GGUF 的常见目录名 / 递归层数（v1.7.69）
UNET_DIR_NAMES = ("diffusion_models", "unet")
UNET_GGUF_MAX_DEPTH = 12


# --------------------------------------------------------------------------- #
# 整卡显存 / 利用率探测（v1.7.58）
# --------------------------------------------------------------------------- #
def _run_quiet(cmd, timeout: float = 4.0) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


_GPU_CACHE = {"t": 0.0, "v": None}


def gpu_probe() -> dict | None:
    """整卡（所有进程加起来）的显存用量与利用率。

    以前只用 `torch.cuda.mem_get_info()`，在这台 AMD（ROCm）服务端上它跟任务管理器/Adrenalin
    对不上：AMD 的显存档位和 torch 报的 free 不是一回事，llama-server 那种独立进程占的显存也容易漏，
    于是面板「运行状态」里的显存就和实际不同步。
    现在按顺序试：nvidia-smi（NVIDIA）→ rocm-smi（AMD，JSON）→ torch 兜底，谁先给出有效数用谁，
    并在返回值里标 source，面板上显示「数据来自哪」。
    探测结果缓存 0.5 秒（面板 1 秒轮询一次，别每次都去拉起子进程）。
    """
    now = time.time()
    if _GPU_CACHE["v"] is not None and now - _GPU_CACHE["t"] < 0.5:
        return _GPU_CACHE["v"]
    v = _gpu_probe_raw()
    _GPU_CACHE["t"] = now
    _GPU_CACHE["v"] = v
    return v


def _gpu_probe_raw() -> dict | None:
    # 1) NVIDIA
    out = _run_quiet(["nvidia-smi",
                      "--query-gpu=memory.used,memory.total,utilization.gpu",
                      "--format=csv,noheader,nounits"])
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            used_mb, total_mb, util = float(parts[0]), float(parts[1]), float(parts[2])
        except Exception:
            continue
        if total_mb > 0:
            return {"used_gb": round(used_mb / 1024, 2),
                    "total_gb": round(total_mb / 1024, 2),
                    "free_gb": round(max(0.0, total_mb - used_mb) / 1024, 2),
                    "util": util, "source": "nvidia-smi"}
    # 2) AMD（ROCm）
    out = _run_quiet(["rocm-smi", "--showmeminfo", "vram", "--showuse", "--json"])
    if out.strip():
        try:
            data = json.loads(out)
        except Exception:
            data = None
        if isinstance(data, dict):
            for card, d in data.items():
                if not isinstance(d, dict):
                    continue
                total_b = used_b = None
                util = None
                for k, v in d.items():
                    kl = k.lower()
                    if "vram total memory" in kl:
                        total_b = _num(v)
                    elif "vram total used memory" in kl:
                        used_b = _num(v)
                    elif "gpu use" in kl:
                        util = _num(v)
                if total_b and used_b is not None:
                    return {"used_gb": round(used_b / 2**30, 2),
                            "total_gb": round(total_b / 2**30, 2),
                            "free_gb": round(max(0.0, total_b - used_b) / 2**30, 2),
                            "util": util if util is not None else None,
                            "source": "rocm-smi"}
    # 3) torch 兜底
    try:
        if torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info()
            if total and total > 0:
                return {"used_gb": round((total - free) / 2**30, 2),
                        "total_gb": round(total / 2**30, 2),
                        "free_gb": round(free / 2**30, 2),
                        "util": None, "source": "torch"}
    except Exception:
        pass
    return None


def _num(v) -> float | None:
    try:
        return float(str(v).strip().split()[0])
    except Exception:
        return None


def _backend_tag() -> str:
    """torch 版本里能看出是 CUDA 还是 ROCm，面板按它显示 [cuda] / [rocm] / [cpu]。"""
    try:
        tv = str(torch.__version__).lower()
    except Exception:
        return "cpu"
    if "rocm" in tv or "hip" in tv:
        return "rocm"
    if "+cu" in tv:
        return "cuda"
    return "cpu"


class ServerError(Exception):
    """可预期的业务错误，会原样回传给 ComfyUI 节点显示。"""


def _to_cpu(obj):
    """递归把容器里的张量搬到 CPU，避免把 GPU 显存句柄序列化出去。"""
    if torch.is_tensor(obj):
        return obj.detach().cpu()
    if isinstance(obj, dict):
        return {k: _to_cpu(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(_to_cpu(v) for v in obj)
    return obj


class Backend:
    def __init__(self, cfg: dict, config_dir: str):
        self.cfg = cfg or {}
        self.config_dir = config_dir
        self.comfy_dir = os.path.abspath(os.path.expanduser(str(self.cfg.get("comfyui_dir") or "")))
        self.models_cfg = self.cfg.get("models") or {}

        max_loaded = self.cfg.get("max_loaded") or {}
        self.max_clip = int(max_loaded.get("clip", 1))
        self.max_vae = int(max_loaded.get("vae", 1))
        self.idle_unload = float(self.cfg.get("idle_unload_seconds", 600) or 0)
        # auto = 交给 ComfyUI 自己决定（有 CUDA 就用 CUDA）；cpu = 强制 CPU（服务端没显卡/显存太小时用）
        self.device = str(self.cfg.get("device") or "auto").strip().lower()

        self._lock = threading.RLock()
        self._loaded = {"clip": OrderedDict(), "vae": OrderedDict(), "clip_vision": OrderedDict(),
                        "unet": OrderedDict()}
        # v1.7.38：参考图「视觉塔结果」/「VAE reference latent」缓存。
        # 图文编码节点会把同一批图连发两次（正向、负向各一次 RPC），第二次别再算一遍。
        self._latent_cache: OrderedDict = OrderedDict()
        self._latent_cache_max = 8
        # v1.7.63：「卸载到内存」——记住哪些缓存的模型被搬到 CPU 了、原来在哪个设备
        self._cpu_off: dict[str, str] = {}
        self.max_cv = 1
        self.max_unet = int(max_loaded.get("unet", 1))
        # 客户端节点上手动指定的 clip_type（覆盖 config.json 里的默认值）；空 = 不覆盖
        self._clip_override: dict[str, str] = {}
        self._clip_applied: dict[str, str] = {}
        self._last_active = time.time()
        self._comfy = None
        self._models_cache = (0.0, None)
        # 远端 ComfyUI 里的 GGUF（提示词优化节点用）：跑在服务端 ComfyUI 的 python 里。
        # 「外部 LLM 服务地址（llm_external）」这套 v1.7.27 就从面板下线了：留着它会让 GGUF 一个都扫不到，
        # 所以配置里残留的 llm_external 一律忽略（不再走 ExternalLLM）。
        if str(self.cfg.get("llm_external") or "").strip():
            print("[服务端] 已忽略 config.json 里残留的 llm_external —— "
                  "现在 GGUF 由本进程自己的 llama-cpp-python 加载，不再转发到外部服务", flush=True)
            self.cfg.pop("llm_external", None)
        self.llm = LLMEngine(self)

        # 模型根目录（可多个）：直接读这些 models 目录里的 vae / clip / text_encoders / clip_vision / LLM，
        # 按文件夹名自动归类，不用再一个个填「CLIP 目录 / VAE 目录 / GGUF 目录」
        self.model_roots = self._norm_roots(self.cfg.get("model_roots"))
        self._root_scan_info = []
        try:
            self._inject_root_models()
        except Exception as e:
            print(f"[服务端] 扫描模型根目录失败：{e}", flush=True)

        if self.idle_unload > 0:
            t = threading.Thread(target=self._idle_loop, name="idle-unloader", daemon=True)
            t.start()

    # ------------------------------------------------------------------ #
    # 初始化 / ComfyUI 导入
    # ------------------------------------------------------------------ #
    def _import_comfy(self):
        if self._comfy is not None:
            return self._comfy
        if not self.comfy_dir or not os.path.isdir(self.comfy_dir):
            raise ServerError(
                f"comfyui_dir 配置无效：{self.comfy_dir!r}。请在 config.json 里写成 B 电脑上 "
                f"ComfyUI 主目录的绝对路径，例如 D:/ComfyUI 或 /home/me/ComfyUI"
            )
        if self.comfy_dir not in sys.path:
            sys.path.insert(0, self.comfy_dir)

        saved_argv = sys.argv[:]
        sys.argv = [sys.argv[0]]  # comfy.cli_args 会 parse_args()，必须屏蔽我们的参数
        try:
            import comfy.sd
            import comfy.utils
            import comfy.clip_vision
            import comfy.model_detection
            import comfy.model_management as mm
            import folder_paths
        except Exception as e:
            raise ServerError(
                f"无法从 {self.comfy_dir} 导入 ComfyUI 的 comfy 模块：{e}\n"
                f"请确认 comfyui_dir 指向的是 ComfyUI 源码根目录（里面有 comfy/ 文件夹和 main.py）"
            ) from e
        finally:
            sys.argv = saved_argv

        self._comfy = {"sd": comfy.sd, "utils": comfy.utils, "mm": mm, "md": comfy.model_detection,
                       "folder_paths": folder_paths, "cv": comfy.clip_vision}
        if self.device == "cpu":
            # ComfyUI 自己的 CPU 开关：设了之后所有 load_models_gpu / get_torch_device 都走 CPU，
            # 完全不建 CUDA context（服务端没显卡、或想给别的程序腾显存时用）。
            try:
                mm.cpu_state = mm.CPUState.CPU
                print("[服务端] 计算设备：CPU（已强制关闭 CUDA 路径）", flush=True)
            except Exception as e:
                print(f"[服务端] 警告：强制 CPU 失败：{e}", flush=True)
        return self._comfy

    # ------------------------------------------------------------------ #
    # 模型清单 / 路径解析
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # 重读配置（界面扫描/改目录后不用重启服务）
    # ------------------------------------------------------------------ #
    def reload(self, cfg: dict) -> dict:
        cfg = cfg or {}
        with self._lock:
            self.cfg = cfg
            cd = str(cfg.get("comfyui_dir") or "").strip()
            if cd:
                self.comfy_dir = os.path.abspath(os.path.expanduser(cd))
            self.models_cfg = cfg.get("models") or {}
            ml = cfg.get("max_loaded") or {}
            if ml.get("clip") is not None:
                self.max_clip = int(ml.get("clip", self.max_clip))
            if ml.get("vae") is not None:
                self.max_vae = int(ml.get("vae", self.max_vae))
            if cfg.get("idle_unload_seconds") is not None:
                self.idle_unload = float(cfg.get("idle_unload_seconds") or 0)
            self._models_cache = (0.0, None)          # 目录改了，模型清单缓存作废
            self.model_roots = self._norm_roots(cfg.get("model_roots"))
            try:
                self._inject_root_models()             # 重新扫一遍模型根目录
            except Exception as e:
                print(f"[服务端] 重扫模型根目录失败：{e}", flush=True)
            try:
                self.llm._models_cache = None      # GGUF 清单也一起作废
            except Exception:
                pass
        return {"comfyui_dir": self.comfy_dir,
                "model_roots": self.model_roots,
                "scanned": len(self._root_scan_info),
                "models": {k: len((self.models_cfg.get(k) or {})) for k in ("clip", "vae", "clip_vision")}}

    # ------------------------------------------------------------------ #
    # 模型根目录 → 自动归类
    # ------------------------------------------------------------------ #
    @staticmethod
    def _norm_roots(v) -> list[str]:
        if isinstance(v, str):
            v = [v]
        out, seen = [], set()
        for r in v or []:
            r = str(r or "").strip().strip('"').strip("'")
            if not r:
                continue
            p = os.path.abspath(os.path.expanduser(r))
            if os.path.isdir(p) and os.path.normcase(p) not in seen:
                seen.add(os.path.normcase(p))
                out.append(p)
        return out

    def _inject_root_models(self):
        """扫 model_roots 里的 vae / clip / text_encoders / clip_vision 文件夹，把文件变成模型条目。
        GGUF 不在这里登记：由 LLMEngine 直接从这些根目录下的 LLM 文件夹扫（见 llm._model_roots）。

        v1.7.54：扫描前先把「扫目录自动登记」的旧条目清一遍 —— 根目录被移除、或模型文件被删掉时，
        这些条目要跟着消失（以前只加不减，删了根目录列表里还挂着一堆指向不存在文件的条目）。
        手写条目（没有 auto 标记）不动。"""
        import config as cfgmod
        self._root_scan_info = []
        self.models_cfg.setdefault("clip", {})
        self.models_cfg.setdefault("vae", {})
        self.models_cfg.setdefault("clip_vision", {})

        # 1) 清掉失效的自动条目（文件没了 / 不在当前任何根目录下）
        try:
            removed = cfgmod.prune_auto_models(self.models_cfg, self.model_roots)
        except Exception as e:
            removed = []
            print(f"[服务端] 清理旧扫描条目失败：{e}", flush=True)
        for kind, name, why in removed:
            print(f"[服务端] 已移除自动条目 {kind}/{name}（{why}）", flush=True)

        if not self.model_roots:
            if removed:
                self._models_cache = (0.0, None)
            return

        # 已经登记过的（含手写条目）按「绝对路径」去重，避免同一个文件出现两份
        known = set()
        for kind in ("clip", "vae", "clip_vision", "unet"):
            for entry in (self.models_cfg.get(kind) or {}).values():
                try:
                    files = self._entry_files(entry)
                except Exception:
                    continue
                for f in files:
                    known.add(os.path.normcase(self._resolve(f)))
        counts = {"clip": 0, "vae": 0, "clip_vision": 0, "llm": 0, "llm_vision": 0}
        for it in cfgmod.scan_roots(self.model_roots):
            kind = it["kind"]
            counts[kind] = counts.get(kind, 0) + 1
            if kind not in ("clip", "vae", "clip_vision"):
                continue
            key_path = os.path.normcase(it["path"])
            if key_path in known:
                continue
            key = cfgmod.entry_name(it["rel"])
            entries = self.models_cfg[kind]
            if key in entries and os.path.normcase(str(
                    (entries[key].get("files") or [""])[0] if isinstance(entries[key], dict) else entries[key])) != key_path:
                i = 2
                while f"{key}_{i}" in entries:
                    i += 1
                key = f"{key}_{i}"
            entries[key] = {"files": [it["path"]], "auto": True,
                            "note": f"目录扫描（{os.path.basename(os.path.dirname(it['root']))}/models）"}
            known.add(key_path)
            self._root_scan_info.append({"kind": kind, "name": key, "path": it["path"]})
        print(f"[服务端] 模型根目录 {len(self.model_roots)} 个 → 自动登记 CLIP {counts['clip']}、VAE {counts['vae']}、"
              f"clip_vision {counts['clip_vision']}、GGUF {counts['llm']}、mmproj {counts['llm_vision']}"
              + (f"；清掉失效条目 {len(removed)} 个" if removed else ""), flush=True)

    def unet_gguf_dirs(self) -> list[str]:
        """扩散模型 GGUF 可能放在这些目录（ComfyUI 官方布局 / 模型根目录里的同名字段）。"""
        dirs, seen = [], set()

        def add(p):
            p = os.path.abspath(p)
            if os.path.isdir(p) and os.path.normcase(p) not in seen:
                seen.add(os.path.normcase(p))
                dirs.append(p)

        bases = list(self.model_roots or [])
        if self.comfy_dir and os.path.isdir(self.comfy_dir):
            bases.append(os.path.join(self.comfy_dir, "models"))
        for b in bases:
            for sub in UNET_DIR_NAMES:
                add(os.path.join(b, sub))
            # 也有人把 GGUF 直接丢在 models/GGUF 下
            add(os.path.join(b, "GGUF"))
        return dirs

    def list_unet_gguf_files(self) -> dict:
        """扫扩散模型目录里的 .gguf（递归，最多 UNET_GGUF_MAX_DEPTH 层），返回 {显示名: 绝对路径}。"""
        out = {}
        for d in self.unet_gguf_dirs():
            for dirpath, dirnames, filenames in os.walk(d):
                depth = dirpath[len(d):].count(os.sep)
                if depth >= UNET_GGUF_MAX_DEPTH:
                    dirnames[:] = []
                dirnames.sort()
                for fn in sorted(filenames):
                    if not fn.lower().endswith(".gguf"):
                        continue
                    p = os.path.join(dirpath, fn)
                    rel = os.path.relpath(p, d).replace(os.sep, "/")
                    out.setdefault(rel, p)
        return out

    def resolve_unet_gguf(self, name: str) -> str:
        """把「远程 GGUF 模型加载器」传进来的名字解析成绝对路径。

        v1.7.69：扩散模型的 gguf 通常放在 models/diffusion_models 或 models/unet，
        以前只按 LLM 目录找，所以列表里根本看不到、选了也报「找不到这个 GGUF」。
        """
        raw = str(name or "").strip()
        if not raw:
            raise ServerError("GGUF 模型名是空的")
        if os.path.isabs(raw) and os.path.exists(raw):
            return raw
        cands = self.list_unet_gguf_files()
        if raw in cands:
            return cands[raw]
        base = os.path.basename(raw.replace("\\", "/"))
        for k, v in cands.items():
            if os.path.basename(k) == base or k.endswith("/" + base):
                return v
        try:  # 兜底：也允许直接选 LLM 目录里的 gguf
            return self.llm.resolve(raw, "llm")
        except Exception:
            raise ServerError(
                f"服务端找不到这个 GGUF 扩散模型：{raw}" + chr(10) +
                f"已经扫过：{'; '.join(self.unet_gguf_dirs()) or '（没有可用目录）'}" + chr(10) +
                "把文件放到服务端 ComfyUI 的 models/diffusion_models（或 models/unet）下，"
                "或在面板「模型根目录」里加一个包含 diffusion_models/unet 的根目录，然后刷新模型列表。")

    def list_models(self, ttl: float = 4.0) -> dict:
        hit = getattr(self, "_models_cache", None)
        if hit and hit[1] is not None and time.time() - hit[0] < ttl:
            return hit[1]
        out = {"clip": {}, "vae": {}, "clip_vision": {}, "unet_gguf": {}}
        for kind in ("clip", "vae"):
            for name, entry in (self.models_cfg.get(kind) or {}).items():
                try:
                    files = self._entry_files(entry)
                except ServerError:
                    out[kind][name] = {"files": [], "exists": False, "note": "配置格式错误"}
                    continue
                out[kind][name] = {
                    "files": files,
                    "resolved": [self._resolve(f) for f in files],
                    "exists": all(os.path.exists(self._resolve(f)) for f in files),
                    "clip_type": (entry or {}).get("clip_type", "stable_diffusion") if kind == "clip" and isinstance(entry, dict) else None,
                    "note": (entry or {}).get("note", "") if isinstance(entry, dict) else "",
                }
        # 视觉编码器（clip_vision）：config.json 里写了的条目 + 目录里现扫到的文件，都列出来给节点下拉
        for name, entry in (self.models_cfg.get("clip_vision") or {}).items():
            try:
                files = self._entry_files(entry)
            except ServerError:
                continue
            out["clip_vision"][name] = {"files": files, "exists": True, "note": "config 条目"}
        for fn in self.list_clip_vision_files():
            key = os.path.basename(fn)
            if key not in out["clip_vision"]:
                out["clip_vision"][key] = {"files": [fn], "exists": True, "note": "目录扫描"}
        # GGUF（给「提示词优化」节点用）：只扫服务端 ComfyUI 的 models/LLM 一类目录
        try:
            gg = self.llm.list_models()
            out.update({"llm": gg.get("llm") or {}, "llm_vision": gg.get("llm_vision") or {}})
        except Exception as e:
            out.update({"llm": {}, "llm_vision": {}, "llm_error": str(e)})
        # 扩散模型 GGUF（给「远程 GGUF 模型加载器」用）：models/diffusion_models、models/unet
        try:
            for name, path in self.list_unet_gguf_files().items():
                out["unet_gguf"][name] = {"files": [path], "resolved": [path],
                                          "exists": True, "note": "目录扫描（扩散模型）"}
        except Exception as e:
            out["unet_gguf_error"] = str(e)
        self._models_cache = (time.time(), out)
        return out

    def clip_vision_dir(self) -> str:
        d = str(self.cfg.get("clip_vision_model_dir") or "").strip()
        if d:
            return os.path.abspath(os.path.expanduser(d))
        return os.path.join(self.comfy_dir, "models", "clip_vision")

    def list_clip_vision_files(self) -> list[str]:
        """扫描 clip_vision 目录里的视觉编码器文件（返回相对该目录的路径）。"""
        root = self.clip_vision_dir()
        if not os.path.isdir(root):
            return []
        exts = (".safetensors", ".sft", ".ckpt", ".pt", ".pth", ".bin")
        found = []
        for dirpath, _dirs, files in os.walk(root):
            if dirpath[len(root):].count(os.sep) > 1:
                _dirs.clear()
                continue
            for fn in files:
                if fn.lower().endswith(exts) and not fn.startswith("put_"):
                    found.append(os.path.relpath(os.path.join(dirpath, fn), root).replace(os.sep, "/"))
        return sorted(found)

    @staticmethod
    def _entry_files(entry) -> list[str]:
        if isinstance(entry, str):
            return [entry]
        if isinstance(entry, dict):
            if entry.get("files"):
                return [str(x) for x in entry["files"]]
            if entry.get("file"):
                return [str(entry["file"])]
        raise ServerError(f"模型配置格式不对：{entry!r}（应为 {{'files': [...]}} 或字符串）")

    def _resolve(self, p: str) -> str:
        p = os.path.expanduser(str(p).strip())
        if os.path.isabs(p):
            return p
        bases = [os.path.join(self.comfy_dir, "models"), self.comfy_dir, self.config_dir]
        # 界面里填的 CLIP / VAE 模型目录（模型放在 ComfyUI 外面时靠它）
        for key in ("clip_model_dir", "vae_model_dir", "clip_vision_model_dir"):
            d = str(self.cfg.get(key) or "").strip()
            if d:
                bases.append(os.path.expanduser(d))
        for base in bases:
            cand = os.path.join(base, p)
            if os.path.exists(cand):
                return cand
        return os.path.join(self.comfy_dir, "models", p)

    def _model_entry(self, kind: str, name: str):
        entries = self.models_cfg.get(kind) or {}
        if name not in entries:
            raise ServerError(
                f"服务端 config.json 没有名为 {name!r} 的 {kind} 模型。已配置：{sorted(entries.keys())}"
            )
        return entries[name]

    # ------------------------------------------------------------------ #
    # 加载 / 缓存
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # v1.7.38 参考图缓存：视觉塔 + VAE latent
    # ------------------------------------------------------------------ #
    @staticmethod
    def _img_key(t) -> str | None:
        """按「图片内容 + 形状」算指纹。同一张图（正/负两条条件）命中同一个 key。"""
        try:
            arr = t.detach().to("cpu", torch.float32).contiguous().numpy()
            return hashlib.blake2b(arr.tobytes(), digest_size=8).hexdigest() + f":{tuple(arr.shape)}"
        except Exception:
            return None

    def _vision_holder(self, clip, max_depth: int = 3):
        """找到真正执行 vision 编码的那个对象（它有 preprocess_embed）。"""
        queue = [(clip, 0)]
        seen = set()
        while queue:
            obj, depth = queue.pop(0)
            if obj is None or id(obj) in seen:
                continue
            seen.add(id(obj))
            if hasattr(obj, "preprocess_embed"):
                return obj
            if depth >= max_depth:
                continue
            for attr in ("cond_stage_model", "clip", "transformer", "model"):
                try:
                    nxt = getattr(obj, attr, None)
                except Exception:
                    nxt = None
                if nxt is not None and not isinstance(nxt, (str, int, float, dict, list, tuple, bool)):
                    queue.append((nxt, depth + 1))
        return None

    def _install_vision_cache(self, clip, model_name: str) -> bool:
        """给这个 CLIP 的视觉塔挂一层「同图同模型」缓存：正/负两条条件只编一次图。
        config.json 里 ``"vision_cache": false`` 可以关掉（默认开）。"""
        if (getattr(self, "cfg", {}) or {}).get("vision_cache") is False:
            return False
        holder = self._vision_holder(clip)
        if holder is None:
            return False
        if getattr(holder, "_rcv_vision_cache", None) is not None:
            return True
        orig = holder.preprocess_embed
        cache: OrderedDict = OrderedDict()
        lock = threading.Lock()

        def preprocess_embed(embed, device=None, **kwargs):
            if not isinstance(embed, dict) or embed.get("type") != "image":
                return orig(embed, device=device, **kwargs)
            key = self._img_key(embed.get("data"))
            hit = None
            if key is not None:
                with lock:
                    hit = cache.get(key)
                    if hit is not None:
                        cache.move_to_end(key)
            if hit is not None:
                merged_c, grid, ds_c = hit
                merged = merged_c.to(device=device, dtype=torch.float32)
                deepstack = [d.to(device=device, dtype=torch.float32) for d in ds_c]
                print(f"[服务端] 视觉塔复用命中（{key[:8]}…），这张图不再重算 vision", flush=True)
                return merged, {"grid": grid, "deepstack": deepstack}
            merged, extra = orig(embed, device=device, **kwargs)
            if key is not None and isinstance(extra, dict) and torch.is_tensor(merged):
                try:
                    with lock:
                        cache[key] = (merged.detach().to("cpu"), extra.get("grid"),
                                      [d.detach().to("cpu") for d in (extra.get("deepstack") or [])])
                        while len(cache) > 4:
                            cache.popitem(last=False)
                except Exception:
                    pass
            return merged, extra

        holder.preprocess_embed = preprocess_embed
        holder._rcv_vision_cache = cache
        print(f"[服务端] 视觉编码缓存已启用（{model_name}）：正向编过的图，负向直接复用", flush=True)
        return True

    def _latent_cached(self, vae_name: str, key: str | None):
        if key is None:
            return None
        with self._lock:
            hit = self._latent_cache.get((vae_name, key))
            if hit is not None:
                self._latent_cache.move_to_end((vae_name, key))
            return hit

    def _latent_store(self, vae_name: str, key: str | None, latent):
        if key is None:
            return
        with self._lock:
            self._latent_cache[(vae_name, key)] = latent.detach().to("cpu")
            while len(self._latent_cache) > self._latent_cache_max:
                self._latent_cache.popitem(last=False)

    def get_clip(self, name: str, clip_type: str = ""):
        with self._lock:
            cache = self._loaded["clip"]
            want = (clip_type or self._clip_override.get(name) or "").strip()
            if name in cache:
                applied = self._clip_applied.get(name, "")
                if not want or want == applied:
                    cache.move_to_end(name)
                    self._last_active = time.time()
                    self._ram_restore("clip", name)   # 之前在内存里躺着的话，先搬回显存
                    return cache[name]
                # 客户端换了 clip_type：先卸掉旧的再按新类型重载
                print(f"[服务端] CLIP {name} 的 clip_type 由 {applied or 'config'} 改为 {want}，重新加载", flush=True)
                self._free(cache.pop(name))
            c = self._import_comfy()
            entry = self._model_entry("clip", name)
            files = [self._resolve(f) for f in self._entry_files(entry)]
            for f in files:
                if not os.path.exists(f):
                    raise ServerError(f"CLIP 文件不存在：{f}")
            cfg_type = entry.get("clip_type", "stable_diffusion") if isinstance(entry, dict) else "stable_diffusion"
            if not want and str(cfg_type).lower() in ("", "auto", "stable_diffusion"):
                # 没指定 / 占位默认值：按权重内容猜（Qwen-VL 这类文本编码器当 stable_diffusion 加载会用错 tokenizer）
                guess = self._detect_clip_type(files, name)
                if guess:
                    cfg_type = guess
                    print(f"[服务端] 自动识别 clip_type={guess}（{os.path.basename(files[0])} 看起来是 {guess} 文本编码器）",
                          flush=True)
            elif want:
                # v1.7.49：显式指定了类型就照办，只是发现与权重对不上时提醒一句（不拦截）
                guess = self._detect_clip_type(files, name)
                if guess and guess.lower() != str(want).lower():
                    print(f"[服务端] 注意：你指定 clip_type={want}，但这个权重看起来是 {guess} —— "
                          f"仍按你指定的加载；若图片进不去/latent 对不上，把它改成「自动」或 {guess}。", flush=True)
            clip_type = self._clip_type(want or cfg_type)
            print(f"[服务端] 加载 CLIP {name}: {files} (clip_type={clip_type})", flush=True)
            t0 = time.time()
            try:
                clip = c["sd"].load_clip(
                    ckpt_paths=files,
                    embedding_directory=c["folder_paths"].get_folder_paths("embeddings"),
                    clip_type=clip_type,
                    model_options={},
                )
            except TypeError:  # 老版本签名
                clip = c["sd"].load_clip(files, c["folder_paths"].get_folder_paths("embeddings"), clip_type)
            print(f"[服务端] CLIP 加载完成，耗时 {time.time() - t0:.1f}s", flush=True)
            self._warn_vram_fit(files, name, "CLIP")
            self._evict("clip", self.max_clip)
            cache[name] = clip
            self._clip_applied[name] = want
            return clip

    def _warn_vram_fit(self, files: list, name: str, kind: str):
        """加载完算一下「权重体积 vs 显存」，超了就当场警告 —— 否则只会表现为
        「GPU 利用率 99% 但出奇地慢 + 共享显存暴涨」，很难看出来是溢出到内存了。"""
        try:
            if not torch.cuda.is_available():
                return
            total = sum(os.path.getsize(f) for f in files if os.path.exists(f)) / 2**30
            # v1.7.58：显存口径统一走 gpu_probe()（AMD 上 torch 报的数不准）
            g = gpu_probe() or {}
            free_gb = float(g.get("free_gb") or 0)
            card_gb = float(g.get("total_gb") or 0)
            if card_gb <= 0:
                return
            weight_gb = total  # safetensors 里通常已是 fp16/bf16，体积≈显存占用
            if weight_gb > card_gb * 0.92 or weight_gb > free_gb * 1.02:
                print(f"[服务端] 警告：{kind}「{name}」权重约 {weight_gb:.1f}GB，"
                      f"这张卡 {card_gb:.1f}GB / 当前空闲 {free_gb:.1f}GB —— 装不下，"
                      f"会被 Windows 拆到「共享显存」(系统内存) 里，每次前向都要走 PCIe。"
                      f"表现就是 GPU 利用率 99%、温度却不高、速度差一个数量级。"
                      f"建议换 fp8/int8 版（约一半体积）或换更大的卡。", flush=True)
            else:
                print(f"[服务端] {kind}「{name}」权重约 {weight_gb:.1f}GB，"
                      f"显存 {card_gb:.1f}GB（空闲 {free_gb:.1f}GB），可以整份放进去", flush=True)
        except Exception:
            pass

    def get_vae(self, name: str):
        with self._lock:
            cache = self._loaded["vae"]
            if name in cache:
                cache.move_to_end(name)
                self._last_active = time.time()
                self._ram_restore("vae", name)
                return cache[name]
            c = self._import_comfy()
            entry = self._model_entry("vae", name)
            files = [self._resolve(f) for f in self._entry_files(entry)]
            for f in files:
                if not os.path.exists(f):
                    raise ServerError(f"VAE 文件不存在：{f}")
            print(f"[服务端] 加载 VAE {name}: {files}", flush=True)
            sd, metadata = c["utils"].load_torch_file(files[0], return_metadata=True)
            try:
                try:
                    vae = c["sd"].VAE(sd=sd, metadata=metadata)
                except TypeError:
                    vae = c["sd"].VAE(sd)
                if hasattr(vae, "throw_exception_if_invalid"):
                    vae.throw_exception_if_invalid()
            except ServerError:
                raise
            except Exception as e:
                raise ServerError(
                    f"{files[0]} 里没有可识别的 VAE：{e}\n"
                    f"排查：① 该文件是独立 VAE 还是整包 checkpoint？整包 checkpoint 不一定带 VAE；"
                    f"② 请改用一个独立的 VAE 文件（如 models/vae/*.safetensors、ae.safetensors、sdxl_vae.safetensors）"
                ) from e
            self._evict("vae", self.max_vae)
            cache[name] = vae
            return vae

    # ------------------------------------------------------------------ #
    # 视觉编码器（clip_vision）：Redux / IPAdapter 那类图像特征
    # ------------------------------------------------------------------ #
    def _resolve_clip_vision(self, name: str) -> str:
        name = str(name or "").strip()
        if name:
            p = os.path.expanduser(name)
            if os.path.isabs(p) and os.path.isfile(p):
                return p
            entry = (self.models_cfg.get("clip_vision") or {}).get(name)
            if entry is not None:
                files = [self._resolve(f) for f in self._entry_files(entry)]
                if not files:
                    raise ServerError(f"clip_vision 条目 {name!r} 没有文件")
                if not os.path.exists(files[0]):
                    raise ServerError(f"clip_vision 文件不存在：{files[0]}")
                return files[0]
            for cand in (os.path.join(self.clip_vision_dir(), name),
                         os.path.join(self.comfy_dir, "models", name),
                         self._resolve(name)):
                if os.path.isfile(cand):
                    return cand
            raise ServerError(
                f"找不到视觉编码器 {name!r}。\n"
                f"已扫描目录：{self.clip_vision_dir()}（{len(self.list_clip_vision_files())} 个文件）。\n"
                f"提示：clip_vision 是 EVA02_CLIP_L_336 这一类**图像编码器**；"
                f"Qwen3-VL / Qwen2.5-VL 那样的是**文本编码器**，用「Qwen-VL 图文混编」模式，别填在这里。\n"
                f"排查：① 把 clip_vision 文件放进上述目录；"
                f"② 或在 config.json 里加 \"clip_vision_model_dir\" 指向存放目录；"
                f"③ 或直接填该文件的绝对路径"
            )
        files = self.list_clip_vision_files()
        if len(files) == 1:
            return os.path.join(self.clip_vision_dir(), files[0])
        if not files:
            raise ServerError(
                f"没有可用的视觉编码器：{self.clip_vision_dir()} 是空的。\n"
                f"说明：「图片 → 服务端 CLIP 视觉」走的是 clip_vision 模型（如 EVA02_CLIP_L_336_psz14_s6B.pt），"
                f"不是文本编码器——Qwen3-VL / Qwen2.5-VL 这类是「文本编码器」，请改用节点的"
                f"「图片 → 服务端文本编码器（Qwen-VL 图文混编）」模式。\n"
                f"确实要用 Redux/IPAdapter 的话：把 clip_vision 模型放进该目录，"
                f"或在 config.json 写 \"clip_vision_model_dir\"，或在节点「视觉模型」里填绝对路径"
            )
        raise ServerError(
            f"视觉编码器目录里有 {len(files)} 个文件：{', '.join(files[:6])}\n"
            f"请在节点「视觉模型」里填其中一个的名字"
        )

    def get_clip_vision(self, name: str = ""):
        path = self._resolve_clip_vision(name)
        with self._lock:
            cache = self._loaded["clip_vision"]
            key = path
            if key in cache:
                cache.move_to_end(key)
                self._last_active = time.time()
                return cache[key]
            c = self._import_comfy()
            if not os.path.exists(path):
                raise ServerError(f"视觉编码器文件不存在：{path}")
            print(f"[服务端] 加载 clip_vision {path}", flush=True)
            t0 = time.time()
            try:
                cv = c["cv"].load(path)
            except Exception as e:
                raise ServerError(
                    f"加载视觉编码器失败：{path}\n{e}\n"
                    f"排查：该文件是否为 clip_vision 类模型（不是文本 CLIP、也不是整包 checkpoint）"
                ) from e
            print(f"[服务端] clip_vision 加载完成，耗时 {time.time() - t0:.1f}s "
                  f"image_size={getattr(cv, 'image_size', '?')}", flush=True)
            self._evict("clip_vision", self.max_cv)
            cache[key] = cv
            return cv

    def clip_vision_encode(self, model: str, images):
        """images: [B,H,W,C] float 0~1（已由 codec 解码好）。"""
        if not torch.is_tensor(images):
            raise ServerError("images 必须是张量")
        if images.dim() == 3:
            images = images.unsqueeze(0)
        if images.shape[-1] > 3:
            images = images[..., :3]
        with self._lock:
            self._last_active = time.time()
            cv = self.get_clip_vision(model)
            try:
                out = cv.encode_image(images.to(torch.float32))
            except Exception as e:
                raise ServerError(f"视觉编码失败：{e}") from e
            payload = {}
            for k in ("image_embeds", "penultimate_hidden_states", "last_hidden_state"):
                v = out[k] if hasattr(out, "__getitem__") else getattr(out, k, None)
                if torch.is_tensor(v):
                    payload[k] = _to_cpu(v)
            if "penultimate_hidden_states" not in payload:
                raise ServerError("视觉编码器没有返回 penultimate_hidden_states，无法用于 Redux 式连接")
            payload["image_sizes"] = [list(s) for s in (getattr(out, "image_sizes", []) or [])]
            payload["model"] = os.path.basename(self._resolve_clip_vision(model))
            return payload

    # ------------------------------------------------------------------ #
    # 参考图 latent（Qwen/Flux Edit 那类 reference_latents）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _fit_pixels(pixels, max_pixels: int = 1048576, multiple: int = 8):
        """按总像素上限等比缩放，宽高对齐到 multiple。"""
        b, h, w, c = pixels.shape
        if max_pixels and max_pixels > 0 and h * w > max_pixels:
            scale = (float(max_pixels) / float(h * w)) ** 0.5
        else:
            scale = 1.0
        nh = max(multiple, int(round(h * scale / multiple)) * multiple)
        nw = max(multiple, int(round(w * scale / multiple)) * multiple)
        if (nh, nw) == (h, w):
            return pixels
        x = pixels.movedim(-1, 1).to(torch.float32)
        x = torch.nn.functional.interpolate(x, size=(nh, nw), mode="bilinear", antialias=True)
        return x.movedim(1, -1).clamp(0.0, 1.0)

    def ref_latent_encode(self, model: str, pixels, max_pixels: int = 1048576, multiple: int = 8):
        if not torch.is_tensor(pixels):
            raise ServerError("pixels 必须是张量")
        if pixels.dim() == 3:
            pixels = pixels.unsqueeze(0)
        if pixels.shape[-1] == 1:
            pixels = pixels.repeat(1, 1, 1, 3)
        pixels = self._fit_pixels(pixels, int(max_pixels or 0), max(8, int(multiple or 8)))
        samples = self.vae_encode(model, pixels)
        return {"samples": samples,
                "height": int(pixels.shape[1]), "width": int(pixels.shape[2])}

    def _detect_clip_type(self, files, name: str = "") -> str:
        """猜 clip_type（不加载权重，几乎零成本）：
        1) 先看 safetensors 里的参数名——Qwen-VL 系带视觉塔（model.visual.* / visual.*）；
        2) 再看文件/条目名——含 qwen + vl / qwen_image 的，就是 Qwen-Image 系文本编码器。
        用 stable_diffusion 加载会拿到 SD1 的 tokenizer，图片永远进不去。"""
        probe = (str(name) + " " + " ".join(os.path.basename(str(f)) for f in files)).lower()
        keys = set()
        try:
            import json as _json
            import struct as _struct
            for f in files:
                if not str(f).lower().endswith(".safetensors") or not os.path.exists(f):
                    continue
                with open(f, "rb") as fh:
                    n = _struct.unpack("<Q", fh.read(8))[0]
                    meta = _json.loads(fh.read(n))
                keys.update(k for k in meta if k != "__metadata__")
        except Exception as e:
            print(f"[服务端] 读取权重参数名失败（不影响启动）：{e}", flush=True)
        if keys and any(k.startswith("model.visual.") or k.startswith("visual.") for k in keys):
            return "qwen_image"  # Qwen-VL 系（含 Qwen-Image 2.1 的 Qwen3-VL-8B）
        if keys and any(k.startswith("conditioner.") for k in keys):
            return "flux"
        if "qwen" in probe and ("vl" in probe or "qwen_image" in probe or "qwen-image" in probe):
            return "qwen_image"
        return ""

    def _clip_type(self, name: str):
        c = self._import_comfy()
        key = str(name or "stable_diffusion").strip().upper().replace("-", "_").replace(" ", "_")
        ct = getattr(c["sd"].CLIPType, key, None)
        if ct is None:
            # v1.7.49：不再遇到不认识的写法就直接失败。
            # ① 先按名字（忽略大小写）在所有枚举里找一遍；
            # ② 找不到再按关键字猜一次（qwen→QWEN_IMAGE、flux→FLUX、sd3/sdxl…）；
            # ③ 实在猜不出来才报错，并把可用列表给它，方便照抄。
            for t in c["sd"].CLIPType:
                if str(t.name).upper() == key:
                    ct = t
                    break
        if ct is None:
            probe = key.lower()
            guess = ""
            for kw_, ty in (("qwen", "QWEN_IMAGE"), ("flux", "FLUX"), ("sd3", "SD3"),
                            ("sdxl", "SDXL"), ("hidream", "HIDREAM"), ("wan", "WAN"),
                            ("chroma", "CHROMA"), ("ltxv", "LTXV"), ("mochi", "MOCHI"),
                            ("cascade", "STABLE_CASCADE"), ("t5", "T5"), ("hunyuan", "HUNYUAN_VIDEO")):
                if kw_ in probe:
                    guess = ty
                    break
            if guess:
                ct = getattr(c["sd"].CLIPType, guess, None)
                if ct is not None:
                    print(f"[服务端] clip_type「{name}」不是标准写法，按关键字当成 {guess} 处理", flush=True)
        if ct is None:
            avail = [x.name.lower() for x in c["sd"].CLIPType]
            raise ServerError(f"未知 clip_type {name!r}，可用：{', '.join(avail)}")
        return ct

    def _evict(self, kind: str, limit: int):
        cache = self._loaded[kind]
        while len(cache) >= max(1, limit):
            old_name, old_obj = cache.popitem(last=False)
            print(f"[服务端] 卸载 {kind} {old_name}（超出 max_loaded={limit}）", flush=True)
            self._free(old_obj)

    def _vram(self):
        """当前显存快照（GB + torch 分配/预留 MB）。v1.7.58 起走 gpu_probe()：
        整卡口径（nvidia-smi / rocm-smi / torch 兜底），和任务管理器、Adrenalin 对得上。"""
        g = gpu_probe()
        try:
            alloc = round(torch.cuda.memory_allocated() / 2**20, 1) if torch.cuda.is_available() else None
            reserved = round(torch.cuda.memory_reserved() / 2**20, 1) if torch.cuda.is_available() else None
        except Exception:
            alloc = reserved = None
        if not g:
            return None
        return {"free_gb": g.get("free_gb"),
                "used_gb": g.get("used_gb"),
                "total_gb": g.get("total_gb"),
                "util": g.get("util"),
                "source": g.get("source"),
                "torch_allocated_mb": alloc,
                "torch_reserved_mb": reserved}

    def _hard_free_gpu(self):
        """真把显存还回去：ComfyUI 的 current_loaded_models 里可能还挂着 CLIP/VAE，
        光把对象从 self._loaded 里 pop 掉，nvidia-smi 里照样占着 —— 必须先让
        model_management 卸掉它自己加载的那份，再强制 empty_cache。"""
        mm = (self._comfy or {}).get("mm")
        if mm is not None:
            try:
                mm.unload_all_models()
            except Exception as e:
                print(f"[服务端] unload_all_models 失败（忽略）：{e}", flush=True)
            try:
                mm.soft_empty_cache(force=True)
            except Exception:
                try:
                    mm.soft_empty_cache()
                except Exception:
                    pass
        try:
            if torch.cuda.is_available():
                torch.cuda.synchronize()
                torch.cuda.empty_cache()
                try:
                    torch.cuda.ipc_collect()
                except Exception:
                    pass
        except Exception:
            pass
        self._torch_cache = True

    def _free(self, obj):
        del obj
        gc.collect()
        self._hard_free_gpu()

    # ------------------------------------------------------------------ #
    # 「卸载到内存」：权重搬到 RAM，显存还回去，但对象留在缓存里
    # ------------------------------------------------------------------ #
    _DEV_KEYS = ("cond_stage_model", "first_stage_model", "clip_model", "visual", "model")

    def _move_weights(self, obj, dev) -> bool:
        """把一个 CLIP / VAE / clip_vision 对象里的权重挪到 dev。
        同时兼顾 ComfyUI 的 ModelPatcher（patcher.model 才是真权重）。"""
        moved = False
        targets = []
        for attr in self._DEV_KEYS:
            sub = getattr(obj, attr, None)
            if isinstance(sub, torch.nn.Module):
                targets.append(sub)
        patcher = getattr(obj, "patcher", None)
        inner = getattr(patcher, "model", None)
        if isinstance(inner, torch.nn.Module) and inner not in targets:
            targets.append(inner)
        for m in targets:
            try:
                m.to(dev)
                moved = True
            except Exception as e:
                print(f"[服务端] 权重搬到 {dev} 失败：{e}", flush=True)
        try:
            if hasattr(obj, "device") and not isinstance(getattr(obj, "device"), str):
                obj.device = torch.device(dev)
        except Exception:
            pass
        return moved

    def _ram_offload(self, kind: str, name: str) -> bool:
        """把一个已加载的模型从显存搬到内存；下次 get_clip/get_vae 时自动搬回来。"""
        obj = self._loaded.get(kind, {}).get(name)
        if obj is None:
            return False
        key = f"{kind}:{name}"
        if key in self._cpu_off:
            return True
        dev = ""
        for attr in ("load_device", "device"):
            d = getattr(obj, attr, None)
            if d is not None and "cuda" in str(d).lower():
                dev = str(d)
                break
        if not dev:
            try:
                dev = str(torch.device("cuda"))
            except Exception:
                return False
        if not self._move_weights(obj, "cpu"):
            return False
        self._cpu_off[key] = dev
        return True

    def _ram_restore(self, kind: str, name: str) -> None:
        key = f"{kind}:{name}"
        dev = self._cpu_off.pop(key, None)
        if not dev:
            return
        obj = self._loaded.get(kind, {}).get(name)
        if obj is None:
            return
        print(f"[服务端] 把 {key} 从内存搬回显存（{dev}），不用重读磁盘", flush=True)
        if not self._move_weights(obj, dev):
            # 搬不回去就老老实实丢掉缓存，下次按正常流程重新加载
            try:
                self._loaded.get(kind, {}).pop(name, None)
            except Exception:
                pass

    def unload(self, kind: str | None = None, name: str | None = None,
               mode: str = "full") -> dict:
        before = self._vram()
        freed = []
        rammed = []
        if (mode or "full").strip() == "ram":
            # 卸载到内存：只把权重搬到 CPU，缓存条目留着（下次不用重读磁盘）
            with self._lock:
                kinds = ([kind] if kind in ("clip", "vae", "clip_vision", "unet")
                         else ["clip", "vae", "clip_vision", "unet"])
                for k in kinds:
                    for n in list(self._loaded[k].keys()):
                        if name and n != name and n != "gguf::" + name:
                            continue
                        if self._ram_offload(k, n):
                            rammed.append(f"{k}:{n}")
                            self._loaded[k].move_to_end(n, last=False)
                gc.collect()
                self._hard_free_gpu()
                self._last_active = time.time() - self.idle_unload
            after = self._vram()
            print(f"[服务端] 卸载到内存 {kind or '全部'}{('/' + name) if name else ''} → "
                  f"搬到内存={rammed or '（缓存里没有）'} 显存 {self._vram_text(before)} → {self._vram_text(after)}",
                  flush=True)
            return {"freed": rammed, "mode": "ram", "vram_before": before, "vram_after": after,
                    "loaded": {k: list(v.keys()) for k, v in self._loaded.items()}}
        with self._lock:
            kinds = [kind] if kind in ("clip", "vae", "clip_vision", "unet") else ["clip", "vae", "clip_vision", "unet"]
            for k in kinds:
                cache = self._loaded[k]
                for n in list(cache.keys()):
                    if name and n != name and n != "gguf::" + name:
                        continue
                    obj = cache.pop(n)
                    self._cpu_off.pop(f"{k}:{n}", None)
                    freed.append(f"{k}:{n}")
                    del obj
            # 不管缓存里有没有 pop 到东西，都要把 ComfyUI 自己那份卸掉（这才是显存真正占着的地方）
            gc.collect()
            self._hard_free_gpu()
            self._latent_cache.clear()
            self._last_active = time.time() - self.idle_unload  # 别让空闲线程又跑一遍
        after = self._vram()
        print(f"[服务端] 卸载 {kind or '全部'}{('/' + name) if name else ''} → freed={freed or '（缓存里没有）'} "
              f"显存 {self._vram_text(before)} → {self._vram_text(after)}", flush=True)
        return {"freed": freed, "mode": "full", "vram_before": before, "vram_after": after,
                "loaded": {k: list(v.keys()) for k, v in self._loaded.items()}}

    @staticmethod
    def _vram_text(v) -> str:
        if not v:
            return "无 CUDA"
        return f"{v['used_gb']}GB / {v['total_gb']}GB"

    def _idle_loop(self):
        while True:
            time.sleep(max(5.0, self.idle_unload / 4.0))
            if time.time() - self._last_active < self.idle_unload:
                continue
            loaded = [f"{k}:{n}" for k, cache in self._loaded.items() for n in cache]
            if not loaded:
                continue
            print(f"[服务端] 空闲超过 {self.idle_unload:.0f}s，释放 {loaded}", flush=True)
            self.unload()

    # ------------------------------------------------------------------ #
    # 操作：CLIP 文本编码
    # ------------------------------------------------------------------ #
    def clip_encode(self, model: str, text: str, last_layer: int = 0, clip_type: str = "",
                    images=None, vision_pixels: int = 1048576, vae: str = "", resolution: int = 0,
                    no_resize: bool = False, keep_vision=None, prevent_empty_text: bool = False,
                    llama_template: str = ""):
        """images: [ [B,H,W,C] 张量, ... ]，每项是一张参考图。

        有图时完全照 ComfyUI 官方 Qwen-Image-Edit 节点（comfy_extras/nodes_qwen.py）的做法：
        图缩放后同时喂「文本编码器 vision 槽」和「VAE latent 槽」；没有 vae 就 keep_vision=True。
        no_resize=True 表示客户端（原生节点）已经把图缩到 32 的倍数，服务端直接按原样用。
        keep_vision 显式给定时以它为准（原生节点会按自己有没有 reference_latents 决定）。"""
        if not isinstance(text, str):
            raise ServerError("text 必须是字符串")
        with self._lock:
            self._last_active = time.time()
            clip = self.get_clip(model, clip_type)
            self._install_vision_cache(clip, model)
            if last_layer:
                clip = clip.clone()
                clip.clip_layer(int(last_layer))  # 与 ComfyUI 的 CLIPSetLastLayer 完全一致

            imgs = []
            if images:
                for g in images:
                    if not torch.is_tensor(g):
                        continue
                    if g.dim() == 3:
                        g = g.unsqueeze(0)
                    if g.shape[-1] > 4:
                        g = g[..., :4]
                    for i in range(g.shape[0]):
                        imgs.append(g[i:i + 1].to(torch.float32))
            if imgs:
                res = int(resolution or 0)
                if res <= 0:
                    res = int(round((int(vision_pixels or 1048576)) ** 0.5))
                return _to_cpu(self._qwen_vl_conditioning(
                    clip, text, imgs, vae, res,
                    resize=not bool(no_resize),
                    keep_vision=keep_vision,
                    prevent_empty_text=bool(prevent_empty_text),
                    llama_template=str(llama_template or "")))

            tokens = clip.tokenize(text)
            scheduled = getattr(clip, "encode_from_tokens_scheduled", None)
            if callable(scheduled):
                out = scheduled(tokens)
            else:
                cond, pooled = clip.encode_from_tokens(tokens, return_pooled=True)
                out = [[cond, {"pooled_output": pooled}]]
            conds = self._canonical_conditioning(out)
        return _to_cpu(conds)

    # ------------------------------------------------------------------ #
    # v1.7.67：自己去盘上借「GGUF 支持」——服务端 exe 不加载 custom_nodes
    # ------------------------------------------------------------------ #
    def _gguf_pack(self):
        """在服务端 ComfyUI 的 custom_nodes 里找到装了 GGUF 支持的包，import 它的 ops / loader。

        v1.7.67 的真问题：exe 里只 `import comfy.sd`，从没加载过 custom_nodes，
        所以用户「明明装了 ComfyUI-GGUF」也白装 —— comfy.sd.load_clip 走的是
        comfy.utils.load_torch_file（torch.load），读 .gguf 直接 weights_only 报错。
        这里把那个包的 ops / loader 自己 import 进来，用 GGMLOps + gguf_clip_loader，
        再走 comfy.sd.load_text_encoder_state_dicts（GGUF 的正确入口）。
        """
        cached = getattr(self, "_gguf_pack_cache", None)
        if cached is not None:
            return cached
        self._import_comfy()  # 先把 comfyui_dir 放进 sys.path，GGUF 包 import 时要用 comfy/folder_paths
        out = {}
        base = os.path.join(self.comfy_dir or "", "custom_nodes")
        try:
            names = sorted(os.listdir(base))
        except Exception:
            names = []
        for d in names:
            if "gguf" not in d.lower():
                continue
            p = os.path.join(base, d)
            if not os.path.isdir(p):
                continue
            has_ops = os.path.exists(os.path.join(p, "ops.py")) \
                or os.path.exists(os.path.join(p, "ops", "__init__.py"))
            has_ldr = os.path.exists(os.path.join(p, "loader.py")) \
                or os.path.exists(os.path.join(p, "loader", "__init__.py"))
            if not (has_ops and has_ldr):
                continue
            pkg = "_rcv_gguf_pack"
            if pkg not in sys.modules:
                m = types.ModuleType(pkg)
                m.__path__ = [p]
                m.__package__ = pkg
                sys.modules[pkg] = m
            mods = {}
            for sub in ("ops", "loader"):
                try:
                    mods[sub] = importlib.import_module(pkg + "." + sub)
                except Exception as e:
                    print(f"[服务端] GGUF 包 {d} 的 {sub} 导入失败：{e}", flush=True)
            ops = getattr(mods.get("ops"), "GGMLOps", None)
            ldr_mod = mods.get("loader")
            sd_loader = getattr(ldr_mod, "gguf_sd_loader", None)
            clip_loader = getattr(ldr_mod, "gguf_clip_loader", None)
            fn_name = ""
            if callable(clip_loader):
                fn_name = "gguf_clip_loader"
            elif callable(sd_loader):
                clip_loader = lambda p, _f=sd_loader: _f(p, is_text_model=True)  # noqa: E731
                fn_name = "gguf_sd_loader(is_text_model=True)"
            if ops is not None and (callable(clip_loader) or callable(sd_loader)):
                out = {"dir": p, "name": d, "ops": ops,
                       "clip_loader": clip_loader, "sd_loader": sd_loader,
                       "fn": fn_name}
                print(f"[服务端] 找到 GGUF 支持：{d}（{fn_name} + GGMLOps）", flush=True)
                break
        self._gguf_pack_cache = out
        if not out:
            print(f"[服务端] custom_nodes 里没找到带 ops/loader 的 GGUF 包：{base}", flush=True)
        return out

    def _gguf_te_diag(self, sd):
        """判断这个 GGUF 到底能不能当 ComfyUI 的文本编码器；不能就给一句能看懂的话。

        v1.7.67 实测发现：Qwen-Image-2.1-PE 这类 GGUF 是 llama.cpp 的大模型格式
        （self_attn.q_projkv 融合权重、ssm_a / attn_gate 这类键），ComfyUI 的 TE 加载器认不出来，
        会悄悄退化成 SDXL 那个 768 维 CLIP —— 不报错但编码结果是垃圾。所以这里提前拦。
        """
        keys = list(sd.keys())
        fused = [k for k in keys
                 if k.endswith("q_projkv.weight") or k.endswith("ssm_a")
                 or k.endswith("attn_gate.weight")]
        if fused:
            return ("这个 GGUF 是 llama.cpp / 大模型（LLM）格式：张量是融合过的"
                    f"（例如 {fused[0]}），ComfyUI 的文本编码器读不了它。" + chr(10) +
                    "它只能当 LLM 用 —— 生成/优化提示词请走「远程 GGUF 加载器 → 远程 GGUF 生成」。" + chr(10) +
                    "要当 CLIP 用，请换一个 ComfyUI 能认的文本编码器 GGUF（键名形如 "
                    "text_model.encoder.layers.0.self_attn.q_proj.weight，或 "
                    "model.layers.0.self_attn.q_proj.weight 这种 q/k/v 分开的），"
                    "或者用「远程 CLIP 加载器」加载 safetensors 版文本编码器。")
        c = self._import_comfy()
        detector = getattr(c["sd"], "detect_te_model", None)
        if callable(detector):
            try:
                if detector(sd) is None:
                    return ("ComfyUI 认不出这个 GGUF 的文本编码器结构（detect_te_model 返回 None），"
                            "装不成 CLIP —— 再装下去只会得到一个 768 维的 SDXL CLIP，编码结果是错的。" + chr(10) +
                            "它多半还是给 llama.cpp 用的 LLM 格式，请改用「远程 GGUF 加载器 → 远程 GGUF 生成」。")
            except Exception:
                pass
        return None

    def _load_text_encoder_gguf(self, path: str, ctype):
        """用 GGUF ops 把 .gguf 文本编码器装成 CLIP；找不到 GGUF 包返回 None。"""
        api = self._gguf_pack()
        if not api or not api.get("clip_loader"):
            return None
        c = self._import_comfy()
        sd = api["clip_loader"](path)
        if isinstance(sd, tuple):
            sd = sd[0]
        if not isinstance(sd, dict) or not sd:
            raise ServerError(f"GGUF 文件里没读出任何权重张量：{os.path.basename(path)}")
        bad = self._gguf_te_diag(sd)
        if bad:
            raise ServerError(bad)
        opts = {"custom_operations": api["ops"]}
        try:
            opts["initial_device"] = c["mm"].text_encoder_offload_device()
        except Exception:
            pass
        return c["sd"].load_text_encoder_state_dicts(
            state_dicts=[sd],
            embedding_directory=c["folder_paths"].get_folder_paths("embeddings"),
            clip_type=ctype,
            model_options=opts,
        )

    def _load_diffusion_gguf(self, path: str):
        """用 GGUF ops 把 .gguf 扩散模型装成 MODEL；找不到 GGUF 包返回 None。

        v1.7.68：这里之前误用了 `gguf_clip_loader`（is_text_model=True），于是把扩散模型的 gguf
        当成"文本模型"去校验 arch，报出 `Unexpected text model architecture type: 'qwen35'`。
        扩散模型必须用 `gguf_sd_loader`（handle_prefix="model.diffusion_model."）。
        """
        api = self._gguf_pack()
        if not api:
            return None
        c = self._import_comfy()
        base = os.path.basename(path)
        sd = None
        if callable(api.get("sd_loader")):
            try:
                out = api["sd_loader"](path)
            except Exception as e:
                msg = str(e).splitlines()[0]
                arch = ""
                if "architecture type in GGUF file" in msg:
                    arch = msg.rsplit(":", 1)[-1].strip().strip("'\"")
                hint = ("这是 LLM / 文本模型格式的 GGUF（arch=%s），不是扩散模型权重，不能当 UNet/DiT 加载。"
                        % (arch or "未知")) if arch else ""
                raise ServerError(
                    f"这个 GGUF 不能当扩散模型加载：{msg}" + chr(10) + hint + chr(10) +
                    "扩散采样要用真正的扩散模型权重（safetensors 的 UNet/DiT，或 ComfyUI 格式的 DiT gguf）；"
                    "提示词增强这类 LLM gguf 只能走「远程 GGUF 加载器 → 远程 GGUF 生成」。") from None
            sd = out[0] if isinstance(out, tuple) else out
        elif callable(api.get("clip_loader")):
            sd = api["clip_loader"](path)
            if isinstance(sd, tuple):
                sd = sd[0]
        if not isinstance(sd, dict) or not sd:
            raise ServerError(f"GGUF 文件里没读出任何权重张量：{base}")
        meta = self._gguf_meta(path)
        if meta:
            print(f"[服务端] GGUF 头信息：arch={meta.get('architecture')} name={meta.get('name')} "
                  f"tensors={meta.get('tensors')}", flush=True)
        self._gguf_unet_diag(sd, base, meta)
        model = None
        # 有些 gguf 的键名多带一层前缀（model. / transformer. / net.），detect_unet_config 就认不出；
        # v1.7.70 起逐个剥掉前缀重试一次，能救的就直接救回来
        prefixes = ["", "model.", "transformer.", "net.", "diffusion_model."]
        last_err = None
        for pre in prefixes:
            if pre:
                if not any(k.startswith(pre) for k in sd):
                    continue
                cand = {k[len(pre):]: v for k, v in sd.items() if k.startswith(pre)}
                if not cand:
                    continue
            else:
                cand = sd
            try:
                detector = c["md"].detect_unet_config if "md" in c else None
                known = None
                if callable(detector):
                    try:
                        known = detector(cand, "")
                    except Exception:
                        known = None
                if pre and known is None:
                    continue  # 这个前缀剥了也认不出，换下一个
                try:
                    model = c["sd"].load_diffusion_model_state_dict(
                        cand, model_options={"custom_operations": api["ops"]})
                except TypeError:
                    model = c["sd"].load_diffusion_model_state_dict(
                        cand, model_options={"custom_operations": api["ops"]}, metadata={})
            except Exception as e:
                last_err = e
                model = None
            if model is not None:
                if pre:
                    print(f"[服务端] 扩散模型键名带前缀「{pre}」，已剥掉后加载成功", flush=True)
                break
        if model is None:
            detail = ""
            if last_err is not None:
                msg = str(last_err).splitlines()[0]
                if "not currently supported" not in msg and "Unknown model architecture" not in msg:
                    detail = msg
            raise ServerError(
                "这个 GGUF 不能当扩散模型加载：" +
                (detail or "ComfyUI 认不出它的模型结构（Unknown model architecture）") + chr(10) +
                self._gguf_unet_unknown(sd, base, meta) + chr(10) +
                "—— 换一个真正的 DiT gguf（ComfyUI-GGUF 的 UnetLoaderGGUF 能加载的那种），"
                "或者直接用 safetensors 版 UNet/DiT。")
        return model

    def _gguf_meta(self, path: str) -> dict:
        """读 GGUF 头部的元信息（arch / name / 张量数）；读不到就返回空 dict，不影响后续流程。"""
        info = {}
        try:
            import gguf  # ComfyUI-GGUF 依赖里有
            reader = gguf.GGUFReader(path)
            for key in ("general.architecture", "general.name", "general.basename"):
                try:
                    f = reader.get_field(key)
                    if f is not None and f.contents() is not None:
                        info[key.split(".")[-1]] = str(f.contents())
                except Exception:
                    pass
            try:
                info["tensors"] = len(reader.tensors)
            except Exception:
                pass
            try:
                info["file_type"] = str(reader.get_field("general.file_type").contents())
            except Exception:
                pass
        except Exception as e:
            info["error"] = str(e).splitlines()[0]
        return info

    def _gguf_key_summary(self, sd, limit: int = 6) -> str:
        """把扩散模型/大模型的张量名按「第一段.第二段」统计，用来一眼看出这是个什么东西。"""
        from collections import Counter
        cnt = Counter()
        for k in list(sd.keys()):
            parts = k.split(".")
            seg = ".".join(parts[:2]) if len(parts) > 1 else parts[0]
            cnt[seg] += 1
        top = cnt.most_common(limit)
        return "、".join(f"{s}({n})" for s, n in top)

    def _gguf_unet_diag(self, sd, name: str = "", meta: dict | None = None):
        """扩散模型的 GGUF 里必须能看到 DiT/UNet 的块；只有 LLM 的层就说明选错文件了。"""
        keys = list(sd.keys())
        meta = meta or {}
        arch = (meta.get("architecture") or "").strip()
        diff_markers = ("double_blocks", "single_blocks", "joint_blocks", "input_blocks",
                        "output_blocks", "transformer_blocks", "blocks.",
                        "txt_in.", "img_in.", "x_embedder.", "proj_out.")
        has_diff = any(any(m in k for m in diff_markers) for k in keys)
        llm_keys = [k for k in keys if k.startswith("model.layers.") or k.endswith("q_projkv.weight")
                    or k in ("model.norm.weight", "model.embed_tokens.weight")
                    or k.startswith(("blk.", "token_embd", "output_norm", "output."))]
        if llm_keys and not has_diff:
            raise ServerError(
                f"{name or '这个 GGUF'} 是 LLM（大模型）格式：里面全是 llama 的 transformer 层"
                f"（例如 {llm_keys[0]}" + (f"，arch={arch}" if arch else "") + "），"
                "没有 DiT/UNet 的 double_blocks / input_blocks / txt_in 之类。" + chr(10) +
                f"键名分布：{self._gguf_key_summary(sd)}" + chr(10) +
                "它不能当扩散模型加载 —— 当 LLM 用请走「远程 GGUF 加载器 → 远程 GGUF 生成」；"
                "要服务端采样，请用真正的扩散模型权重（safetensors UNet/DiT，或 ComfyUI 格式的 DiT gguf）。")

    def _gguf_unet_unknown(self, sd, name: str, meta: dict) -> str:
        """ComfyUI 的 detect_unet_config 认不出这个 GGUF 时，拼一段能定位问题的中文说明。"""
        arch = (meta.get("architecture") or "未知").strip()
        tensors = meta.get("tensors") or len(sd)
        lines = [f"ComfyUI 认不出这个 GGUF 的扩散模型结构（detect_unet_config 返回 None，未知架构）。",
                 f"  general.architecture = {arch}",
                 f"  张量数 = {tensors}",
                 f"  键名分布 = {self._gguf_key_summary(sd)}"]
        known_llm = ("qwen3vl", "qwen35", "qwen2", "qwen3", "llama", "gemma", "mistral",
                     "phi", "clip", "t5", "umt5")
        if any(a in arch.lower() for a in known_llm):
            lines.append("  这个 arch 属于「文本编码器 / 大语言模型」，不是扩散模型（DiT/UNet）。")
            lines.append("  它的正确用法是：当提示词模型 →「远程 GGUF 加载器 → 远程 GGUF 生成」；"
                         "当文本编码器 → 需要 ComfyUI 能认的 TE gguf（这一类多半也认不出）。")
        else:
            lines.append("  这不是 ComfyUI 现在支持的 DiT 架构：可能是别的工具（sd.cpp / ComfyUI 新模型）"
                         "专用的 gguf，或者被改名/截断的文件。")
            lines.append("  建议核对文件名与来源：只有「ComfyUI-GGUF 能加载的 DiT gguf」或者 safetensors 版 "
                         "UNet/DiT 才能进「远程 GGUF 模型加载器」。")
        return chr(10).join(lines)

    # ------------------------------------------------------------------ #
    # v1.7.64：把 .gguf 当「扩散模型（DiT/UNet）」加载 + 采样也在服务端跑
    # ------------------------------------------------------------------ #
    def get_unet_gguf(self, name: str):
        """用服务端 ComfyUI 的 comfy.sd 把 GGUF 权重加载成 MODEL（等价于 ComfyUI-GGUF 的 UnetLoaderGGUF）。"""
        c = self._import_comfy()
        path = self.resolve_unet_gguf(name)
        if not os.path.exists(path):
            raise ServerError(f"GGUF 扩散模型文件不存在：{path}")
        # 缓存键按「绝对路径」算：下拉里写相对名或只写文件名，都命中同一份，不会加载两次
        key = "gguf::" + os.path.normcase(path)
        with self._lock:
            cache = self._loaded["unet"]
            if key in cache:
                cache.move_to_end(key)
                self._last_active = time.time()
                self._ram_restore("unet", key)
                return cache[key]
            print(f"[服务端] 加载 GGUF 扩散模型 {name}: {path}", flush=True)
            t0 = time.time()
            model = None
            errs = []
            try:
                model = self._load_diffusion_gguf(path)
            except ServerError:
                raise
            except Exception as e:
                errs.append(str(e).splitlines()[0])
            if model is None:
                try:
                    model = c["sd"].load_diffusion_model(unet_path=path, model_options={})
                except Exception as e:
                    errs.append(str(e).splitlines()[0])
            if model is None:
                try:
                    model = c["sd"].load_diffusion_model(path, model_options={})
                except Exception as e:
                    errs.append(str(e).splitlines()[0])
            if model is None:
                msg = " | ".join(errs)[:600]
                raise ServerError(
                    f"服务端 ComfyUI 读不了这个 GGUF 扩散模型：{msg}" + chr(10) +
                    "本版会自动去 custom_nodes 里借 GGUF 包（ComfyUI-GGUF 的 ops.py + loader.py）；"
                    "确认那个目录里两样都在，然后重启服务端 exe 再试。")
            print(f"[服务端] GGUF 扩散模型加载完成，耗时 {time.time() - t0:.1f}s", flush=True)
            self._warn_vram_fit([path], name, "GGUF 扩散模型")
            self._evict("unet", self.max_unet)
            cache[key] = model
            return model

    def _cond_to_dev(self, conds, dev):
        """把客户端传来的 CONDITIONING 搬到采样设备。"""
        if not conds:
            return []
        out = []
        for item in conds:
            t, d = item[0], (item[1] if len(item) > 1 else {}) or {}
            out.append([t.to(dev) if torch.is_tensor(t) else t,
                        {k: (v.to(dev) if torch.is_tensor(v) else v) for k, v in d.items()}])
        return out

    def unet_sample(self, model: str, latent, positive, negative, steps: int = 20,
                    cfg: float = 5.0, sampler_name: str = "euler", scheduler: str = "normal",
                    seed: int = 0, denoise: float = 1.0):
        """在服务端用 GGUF 扩散模型跑一次 KSampler 等价采样，只把 latent 结果回传。"""
        with self._lock:
            self._last_active = time.time()
            m = self.get_unet_gguf(model)
            self._import_comfy()
            import comfy.sample
            import comfy.samplers  # noqa: F401
            lat = latent.get("samples", latent) if isinstance(latent, dict) else latent
            if not torch.is_tensor(lat):
                raise ServerError('latent 必须是张量或 {"samples": 张量}')
            dev = getattr(m, "load_device", None) or torch.device(
                "cuda" if torch.cuda.is_available() else "cpu")
            lat = lat.to(dev)
            pos = self._cond_to_dev(positive, dev)
            neg = self._cond_to_dev(negative, dev) if negative else []
            if not pos:
                raise ServerError("正向条件为空：把条件节点的输出接到「正向条件」上")
            from comfy.model_management import throw_exception_if_processing_interrupted
            noise = comfy.sample.prepare_noise(lat, int(seed))
            t0 = time.time()
            samples = comfy.sample.sample(
                m, noise, int(steps), float(cfg), str(sampler_name), str(scheduler),
                pos, neg, lat, denoise=float(denoise), noise_mask=None,
                callback=None, disable_pbar=True, seed=int(seed),
            )
            throw_exception_if_processing_interrupted()
            secs = round(time.time() - t0, 2)
            print(f"[服务端] 采样完成：{model} | {sampler_name}/{scheduler} | {steps} 步 | "
                  f"{secs}s | latent {tuple(samples.shape)}", flush=True)
        return {"samples": samples.detach().cpu(), "seconds": secs}

    def get_clip_gguf(self, name: str, clip_type: str = ""):
        """v1.7.62：把 LLM 目录里的 .gguf 当「文本编码器」加载成 CLIP（Qwen2.5-VL 这类）。

        走的是 ComfyUI 自己的 sd.load_clip：装了 ComfyUI-GGUF（city96）那套 ops 才能读 GGUF；
        没装会抛 Unknown op / 不认识的文件头 —— 这里翻译成一句人能看懂的话。
        """
        key = "gguf::" + str(name)
        want = (clip_type or "").strip()
        with self._lock:
            cache = self._loaded["clip"]
            applied = self._clip_applied.get(key, "")
            if key in cache and (not want or want == applied):
                cache.move_to_end(key)
                self._last_active = time.time()
                self._ram_restore("clip", key)
                return cache[key]
            if key in cache:
                print(f"[服务端] GGUF-CLIP {name} 的 clip_type 改为 {want or 'config'}，重新加载", flush=True)
                self._free(cache.pop(key))
            c = self._import_comfy()
            try:
                path = self.llm.resolve(name, "llm")
            except Exception as e:
                raise ServerError(f"服务端找不到这个 GGUF：{name}（{e}）")
            if not os.path.exists(path):
                raise ServerError(f"GGUF 文件不存在：{path}")
            ctype = self._clip_type(want or "qwen_image")
            print(f"[服务端] 加载 GGUF 文本编码器 {os.path.basename(path)} (clip_type={ctype})", flush=True)
            t0 = time.time()
            clip = None
            errs = []
            try:
                clip = self._load_text_encoder_gguf(path, ctype)
            except Exception as e:
                errs.append(str(e).splitlines()[0])
            if clip is None:
                # 没找到 GGUF 包：再试原路径（万一那一版 ComfyUI 的 load_torch_file 自己认 gguf）
                try:
                    clip = c["sd"].load_clip(
                        ckpt_paths=[path],
                        embedding_directory=c["folder_paths"].get_folder_paths("embeddings"),
                        clip_type=ctype,
                        model_options={},
                    )
                except TypeError:
                    try:
                        clip = c["sd"].load_clip([path], c["folder_paths"].get_folder_paths("embeddings"), ctype)
                    except Exception as e:
                        errs.append(str(e).splitlines()[0])
                except Exception as e:
                    errs.append(str(e).splitlines()[0])
            if clip is None:
                msg = " | ".join([e for e in errs if e])[:600]
                raise ServerError(
                    f"服务端没能把 .gguf 文本编码器装成 CLIP：{msg}" + chr(10) +
                    f"已经做的：在 {os.path.join(self.comfy_dir or '', 'custom_nodes')} 里找带 "
                    f"ops.py/loader.py 的 GGUF 包（ComfyUI-GGUF / ComfyUI-GGUF-VLM 之类），用它自己的 "
                    f"GGMLOps + gguf_clip_loader 走 load_text_encoder_state_dicts。" + chr(10) +
                    "若日志里写着「没找到带 ops/loader 的 GGUF 包」，就去服务端 ComfyUI 的 custom_nodes "
                    "装 city96/ComfyUI-GGUF，确认目录里同时有 ops.py（或 ops/）和 loader.py；"
                    "然后重启本 exe。")
            print(f"[服务端] GGUF 文本编码器加载完成，耗时 {time.time() - t0:.1f}s", flush=True)
            self._warn_vram_fit([path], name, "GGUF-CLIP")
            self._evict("clip", self.max_clip)
            cache[key] = clip
            self._clip_applied[key] = want
            return clip

    def gguf_encode(self, model: str, text: str, clip_type: str = "", last_layer: int = 0,
                    images=None, keep_vision=None, llama_template: str = ""):
        """用 GGUF 文本编码器编码文本 → CONDITIONING。

        参考图这条路不支持：GGUF 的视觉塔（mmproj）ComfyUI 侧没法按 sd.load_clip 装进去，
        要带图请用「远程 CLIP 图文编码」（它走 sd.load_clip 的 safetensors 文本编码器）。
        """
        if not isinstance(text, str):
            raise ServerError("text 必须是字符串")
        if images:
            raise ServerError("GGUF 文本编码器这条链路不支持参考图（视觉塔没法用 sd.load_clip 装）。\n"
                              "要带图请改用「远程 CLIP 图文编码」节点（safetensors 文本编码器 + 可选 VAE）。")
        with self._lock:
            self._last_active = time.time()
            clip = self.get_clip_gguf(model, clip_type)
            if last_layer:
                clip = clip.clone()
                clip.clip_layer(int(last_layer))
            tokens = clip.tokenize(text)
            scheduled = getattr(clip, "encode_from_tokens_scheduled", None)
            if callable(scheduled):
                out = scheduled(tokens)
            else:
                cond, pooled = clip.encode_from_tokens(tokens, return_pooled=True)
                out = [[cond, {"pooled_output": pooled}]]
            conds = self._canonical_conditioning(out)
        return _to_cpu(conds)

    def _qwen_vl_conditioning(self, clip, text: str, imgs: list, vae_name: str = "", resolution: int = 1024,
                              resize: bool = True, keep_vision=None, prevent_empty_text: bool = False,
                              llama_template: str = ""):
        """照抄 comfy_extras/nodes_qwen.py 的 Qwen-Image-Edit 逻辑：
        参考图按 resolution² 等比缩放到 32 的倍数，同一张图既做 vision 输入又做 VAE latent，
        这样每个 vision slot 正好覆盖 2×2 个 latent。
        resize=False：客户端（原生节点）已经缩好了，直接用。"""
        images_vl, ref_latents = [], []
        vae_model = self.get_vae(vae_name) if vae_name else None
        res = max(0, int(resolution or 0))
        for img in imgs:
            s = img[:1]
            if resize:
                s = s.permute(0, 3, 1, 2)  # B,C,H,W
                if res > 0:
                    ratio = s.shape[3] / s.shape[2]
                    width = round(math.sqrt(res * res * ratio) / 32) * 32
                    height = round(math.sqrt(res * res / ratio) / 32) * 32
                else:
                    width, height = round(s.shape[3] / 32) * 32, round(s.shape[2] / 32) * 32
                width, height = max(32, width), max(32, height)
                if (width, height) == (s.shape[3], s.shape[2]):
                    s = img[:1]
                else:
                    s = self._comfy["utils"].common_upscale(
                        s, int(width), int(height), "lanczos", "disabled").permute(0, 2, 3, 1)
            rgb = s[..., :3]
            if s.shape[-1] > 3:  # 透明图：视觉塔按白底合成，VAE 保留 alpha
                a = s[..., 3:]
                rgb = rgb * a + (1.0 - a)
            images_vl.append(rgb.float())
            if vae_model is not None:
                key = self._img_key(s)
                hit = self._latent_cached(vae_name, key)
                if hit is not None:
                    print(f"[服务端] reference latent 复用命中（{(key or '')[:8]}…），不再跑 VAE 编码", flush=True)
                    ref_latents.append(hit)
                else:
                    t_lat = time.time()
                    lat = vae_model.encode(s.float())
                    self._latent_store(vae_name, key, lat)
                    ref_latents.append(lat)
                    print(f"[服务端] VAE 编码 reference latent：{time.time() - t_lat:.2f}s", flush=True)

        if keep_vision is None:
            # 跟原生 tokenizer 的默认一致（QwenImage21Tokenizer: keep_vision=False）。
            # 之前这里按「服务端没编 latent 就 True」推断，结果原生 2.0 节点（不传 keep_vision、
            # 自己挂 reference_latents）会变成「vision token 留一份 + latent 再挂一份」，双重插入 → 出图糊/重影。
            keep_vision = False
        keep_vision = bool(keep_vision)
        try:
            # 不自己拼 <|im_start|> 模板：Qwen-Image 2.1 的 tokenizer 会按图数重复 vision 槽并记录 image_slots
            t_tok = time.time()
            tokens = clip.tokenize(text, images=images_vl, keep_vision=keep_vision,
                                   prevent_empty_text=prevent_empty_text,
                                   llama_template=(llama_template or None))
        except TypeError as e:
            raise ServerError(
                f"这个文本编码器不接受图片输入（tokenize 不支持 images=）：{e}\n"
                f"只有 Qwen-VL 系（qwen_image / Qwen3-VL 文本编码器）能这样用。"
                f"若是普通 CLIP / T5，请改用「参考图 → 服务端 VAE 编码」或「图片 → 服务端 clip_vision」模式"
            ) from e
        except Exception as e:
            raise ServerError(f"图文混编失败：{e}") from e

        def _is_img(t):
            if isinstance(t, dict):
                return t.get("type") == "image"
            if isinstance(t, (tuple, list)) and len(t) > 0 and isinstance(t[0], dict):
                return t[0].get("type") == "image"
            return False

        hits = 0
        try:
            groups = tokens.values() if isinstance(tokens, dict) else [tokens]
            hits = sum(1 for grp in groups for row in grp for t in row if _is_img(t))
        except Exception:
            hits = len(images_vl)
        if hits < len(images_vl):
            print(f"[服务端] 警告：只命中 {hits}/{len(images_vl)} 个 vision token —— "
                  f"这个 CLIP 的 tokenizer 不认识 vision 占位符。检查 config.json 里该条目的 "
                  f"\"clip_type\" 是否为 qwen_image（Qwen-Image / Qwen3-VL 系必须用它；"
                  f"填 stable_diffusion 会按 SD1.5 的 tokenizer 处理，图片进不去）", flush=True)

        scheduled = getattr(clip, "encode_from_tokens_scheduled", None)
        t_enc = time.time()
        if callable(scheduled):
            out = scheduled(tokens)
        else:
            cond, pooled = clip.encode_from_tokens(tokens, return_pooled=True)
            out = [[cond, {"pooled_output": pooled}]]
        print(f"[服务端] 耗时：tokenize {t_enc - t_tok:.2f}s + 编码 {time.time() - t_enc:.2f}s"
              f"（{len(images_vl)} 张图 / {hits} vision token）", flush=True)
        conds = self._canonical_conditioning(out)
        if ref_latents:
            conds = self._append_cond_values(conds, "reference_latents", ref_latents)
            h, w = ref_latents[0].shape[-2] * 16, ref_latents[0].shape[-1] * 16
            print(f"[服务端] 图文混编：{len(images_vl)} 张图 / {hits} 个 vision token / keep_vision={keep_vision}，"
                  f"挂 {len(ref_latents)} 份 reference_latents（采样尺寸建议 {w}x{h}）", flush=True)
        else:
            print(f"[服务端] 图文混编：{len(images_vl)} 张图 / {hits} 个 vision token / keep_vision={keep_vision}"
                  + ("（客户端自带 reference_latents，服务端不再编）" if not keep_vision
                     else "（图像特征直接进条件）"), flush=True)
            if not keep_vision:
                print("[服务端] 注意：keep_vision=False 但没有 reference_latents，"
                      "参考图不会出现在结果里（客户端应把 VAE 编码的 latent 一起挂上条件）", flush=True)
        return conds

    @staticmethod
    def _append_cond_values(conds, key: str, values: list):
        """等价 node_helpers.conditioning_set_values(..., append=True)"""
        out = []
        for c, d in conds:
            d = dict(d or {})
            d[key] = list(d.get(key) or []) + list(values)
            out.append([c, d])
        return out

    @staticmethod
    def _canonical_conditioning(out) -> list:
        """统一成 ComfyUI 的 CONDITIONING 规范：[ [tensor, dict], ... ]"""
        if isinstance(out, dict):
            if "cond" in out or "conditioning" in out:
                out = [out]
            else:
                out = [[out, {}]]
        if torch.is_tensor(out):
            out = [[out, {}]]
        result = []
        for item in out:
            if torch.is_tensor(item):
                result.append([item, {}])
                continue
            if isinstance(item, dict):
                d = dict(item)
                cond = d.pop("cond", None)
                if cond is None:
                    cond = d.pop("conditioning", None)
                if cond is None:
                    raise ServerError(f"CLIP 输出结构无法识别：keys={list(d.keys())}")
                result.append([cond, {k: v for k, v in d.items() if v is not None}])
                continue
            if isinstance(item, (list, tuple)) and len(item) == 2 and torch.is_tensor(item[0]):
                result.append([item[0], dict(item[1] or {})])
                continue
            raise ServerError(f"CLIP 输出结构无法识别：{type(item)!r}")
        if not result:
            raise ServerError("CLIP 编码结果为空")
        return result

    # ------------------------------------------------------------------ #
    # 操作：VAE
    # ------------------------------------------------------------------ #
    def vae_decode(self, model: str, samples, tiled: bool = False, tile_size: int = 512, overlap: int = 64):
        if not torch.is_tensor(samples):
            raise ServerError("latent 必须是张量")
        if samples.dim() == 3:
            samples = samples.unsqueeze(0)
        with self._lock:
            self._last_active = time.time()
            vae = self.get_vae(model)
            ratio = self._vae_ratio(vae)
            expect_ch = int(getattr(vae, "latent_channels", 4) or 4)
            if samples.shape[1] != expect_ch:
                raise ServerError(
                    f"VAE 解码失败：latent 有 {samples.shape[1]} 个通道，但该 VAE 需要 {expect_ch} 个。\n"
                    f"通常说明本机的图像模型与服务端这个 VAE 不是同一代模型（例如 SD1.5 的 4 通道 latent 配了 16 通道的 Flux/Qwen VAE），"
                    f"请换成与本机 UNet 同源的 VAE。"
                )
            samples = samples.to(dtype=self._vae_dtype(vae))
            # 视频 VAE（Wan / Qwen-Image / HunyuanVideo…）要 5 维 [B,C,T,H,W]；单帧时补一个 T=1
            latent_in = samples
            if self._is_video_vae(vae) and samples.dim() == 4:
                latent_in = samples.unsqueeze(2)
            try:
                if tiled:
                    tx = max(64, int(tile_size // ratio))
                    ov = max(8, int(overlap // ratio))
                    try:
                        images = vae.decode_tiled(latent_in, tile_x=tx, tile_y=tx, overlap=ov)
                    except TypeError:
                        images = vae.decode_tiled(latent_in, tile_x=tx, tile_y=tx)
                else:
                    images = vae.decode(latent_in)
            except Exception as e:
                raise ServerError(
                    f"VAE 解码失败：{e}\n排查：服务端显存不足（勾 use_tiled / 调小 tile_size）、"
                    f"或 latent 与该 VAE 不匹配（跨机模型不同源）；视频 VAE 需要 5 维 latent，已尝试自动补 T=1"
                ) from e
            if isinstance(images, dict):  # 兼容极少数自定义 VAE 的返回
                images = images.get("samples", images)
            if torch.is_tensor(images) and images.dim() == 5:  # [B,T,H,W,C] -> 取首帧
                images = images[:, 0]
            if torch.is_tensor(images) and images.dim() == 3:
                images = images.unsqueeze(0)
            self._last_active = time.time()
            return _to_cpu(images)

    def vae_encode(self, model: str, pixels, tiled: bool = False, tile_size: int = 512, overlap: int = 64):
        if not torch.is_tensor(pixels):
            raise ServerError("pixels 必须是张量")
        if pixels.dim() == 3:
            pixels = pixels.unsqueeze(0)
        if pixels.shape[-1] == 1:
            pixels = pixels.repeat(1, 1, 1, 3)
        with self._lock:
            self._last_active = time.time()
            vae = self.get_vae(model)
            pixels = pixels.to(dtype=torch.float32)
            # 视频 VAE 优先按 [B,T,H,W,C]（单帧 T=1）传，失败再退回 4 维
            variants = [pixels]
            if self._is_video_vae(vae) and pixels.dim() == 4:
                variants.insert(0, pixels.unsqueeze(1))
            samples, last_err = None, None
            for pin in variants:
                try:
                    if tiled:
                        tx = max(64, int(tile_size))
                        ov = max(8, int(overlap))
                        try:
                            samples = vae.encode_tiled(pin, tile_x=tx, tile_y=tx, overlap=ov)
                        except TypeError:
                            samples = vae.encode_tiled(pin, tile_x=tx, tile_y=tx)
                    else:
                        samples = vae.encode(pin)
                    break
                except Exception as e:
                    last_err = e
            if samples is None:
                raise ServerError(
                    f"VAE 编码失败：{last_err}\n排查：服务端显存不足（勾 use_tiled）、"
                    f"或该 VAE 与本机图像模型不同源"
                )
            if isinstance(samples, dict):
                samples = samples.get("samples", samples)
            if torch.is_tensor(samples) and samples.dim() == 5:  # [B,C,T,H,W] -> 单帧
                samples = samples[:, :, 0]
            return _to_cpu(samples)

    @staticmethod
    def _is_video_vae(vae) -> bool:
        """Wan / Qwen-Image / HunyuanVideo 这类 VAE 的 downscale_ratio 是 (fn, h, w) 元组。"""
        return isinstance(getattr(vae, "downscale_ratio", None), (tuple, list))

    @staticmethod
    def _vae_ratio(vae) -> int:
        dr = getattr(vae, "downscale_ratio", None)
        if isinstance(dr, (int, float)) and dr >= 1:
            return int(round(float(dr)))
        if isinstance(dr, (tuple, list)):  # 视频 VAE：取空间比例
            for x in dr[1:]:
                if isinstance(x, (int, float)) and x >= 1:
                    return int(x)
        for attr in ("downscale_scale",):
            v = getattr(vae, attr, None)
            if isinstance(v, (int, float)) and v >= 1:
                return int(round(float(v)))
        return 8

    @staticmethod
    def _vae_dtype(vae):
        try:
            return vae.vae_dtype()
        except Exception:
            pass
        for attr in ("dtype", "vae_dtype"):
            v = getattr(vae, attr, None)
            if isinstance(v, torch.dtype):
                return v
        return torch.float32

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def status(self, light: bool = False) -> dict:
        info = {
            "comfyui_dir": self.comfy_dir,
            "comfy_imported": self._comfy is not None,
            "loaded": {k: list(v.keys()) for k, v in self._loaded.items()},
            "max_loaded": {"clip": self.max_clip, "vae": self.max_vae},
            "idle_unload_seconds": self.idle_unload,
            "torch": torch.__version__,
            "cuda_available": torch.cuda.is_available(),
            "device": "cpu" if self.device == "cpu" else ("cuda" if torch.cuda.is_available() else "cpu"),
            "device_name": None,
            "vram": None,
            "torch_allocated_mb": None,
            "torch_reserved_mb": None,
            "models": ({} if light else self.list_models()),
            "llm": self.llm.status(light=light),
        }
        try:
            if torch.cuda.is_available():
                info["device_name"] = torch.cuda.get_device_name(0)
            else:
                info["device_name"] = "CPU"
        except Exception:
            pass
        # v1.7.58：显存统一走 gpu_probe()（nvidia-smi / rocm-smi / torch），并把 torch 进程内占用一起带上，
        # 面板就能同时显示「整卡」和「本进程」两行，不再出现与实际不同步的情况
        v = self._vram()
        if v:
            info["vram"] = v
            info["torch_allocated_mb"] = v.get("torch_allocated_mb")
            info["torch_reserved_mb"] = v.get("torch_reserved_mb")
            info["gpu_backend"] = v.get("source")
        info["gpu_backend"] = info.get("gpu_backend") or _backend_tag()
        return info

    def handle(self, op: str, params: dict) -> dict:
        """统一入口：返回可被 codec.pack 的 Python 对象。"""
        params = params or {}
        if op in ("ping", "status"):
            return self.status()
        if op == "models":
            return self.list_models()
        if op == "load":
            kind = params.get("kind")
            name = params.get("model") or params.get("name")
            if kind == "clip":
                ctype = str(params.get("clip_type") or "").strip()
                if ctype:
                    self._clip_override[name] = ctype
                self.get_clip(name, ctype)
            elif kind == "vae":
                self.get_vae(name)
            else:
                raise ServerError("load 的 kind 必须是 clip 或 vae")
            return {"loaded": {k: list(v.keys()) for k, v in self._loaded.items()}}
        if op == "unload":
            return self.unload(params.get("kind"), params.get("model"),
                               mode=str(params.get("mode") or "full"))
        if op == "llm_offload_ram":
            # 「卸载到内存」的 LLM 版：同一支 GGUF 以 0 层上卡重载 —— 权重进内存、显存还回去，
            # 下次 llm_chat/llm_load 会按节点自己的「上卡层数」再搬回显存（读盘走系统缓存，很快）。
            st = self.llm.status(light=True) or {}
            name = str(st.get("model") or "")
            if not name or str(st.get("state") or "") in ("", "未启动", "未加载", "MOCK"):
                return {"moved": [], "note": "服务端当前没有已加载的 GGUF，跳过"}
            nctx = int(st.get("n_ctx") or 4096)
            before = self._vram()
            try:
                self.llm.load(name, str(st.get("mmproj") or ""), nctx, 0)
            except Exception as e:
                return {"moved": [], "note": f"搬到内存失败（保持原状）：{e}"}
            after = self._vram()
            st2 = self.llm.status(light=True) or {}
            layers = st2.get("n_gpu_layers")
            note = "GGUF 权重已在内存（0 层上卡），显存已释放"
            if layers is not None and int(layers) != 0:
                note = (f"注意：0 层上卡没生效（服务端报告的层数是 {layers}）——"
                        f"多半是服务端 exe 还是旧版，换成本版包里的 exe 再试")
            print(f"[服务端] GGUF {name} 卸载到内存（0 层上卡）→ 显存 "
                  f"{self._vram_text(before)} → {self._vram_text(after)} | {note}", flush=True)
            return {"moved": [name], "mode": "ram", "vram_before": before, "vram_after": after,
                    "n_gpu_layers": layers, "note": note}
        if op == "vae_info":
            vae = self.get_vae(params.get("model", ""))
            return {
                "latent_channels": int(getattr(vae, "latent_channels", 4) or 4),
                "downscale_ratio": self._vae_ratio(vae),
                "model": params.get("model", ""),
            }
        if op == "clip_encode":
            model = params.get("model", "")
            ctype = str(params.get("clip_type") or "").strip()
            if ctype:
                self._clip_override[model] = ctype
            imgs = params.get("images")
            if imgs is None and params.get("image_frames"):
                imgs = [params["image_frames"]]
            if imgs:
                imgs = [codec.png_frames_to_tensor(g) for g in imgs if g]
            conds = self.clip_encode(model, str(params.get("text", "")),
                                     int(params.get("last_layer", 0) or 0), ctype,
                                     images=imgs or None,
                                     vision_pixels=int(params.get("vision_pixels", 1048576) or 0),
                                     vae=str(params.get("vae", "") or ""),
                                     resolution=int(params.get("resolution", 0) or 0),
                                     no_resize=bool(params.get("no_resize", False)),
                                     keep_vision=(None if params.get("keep_vision") is None
                                                  else bool(params.get("keep_vision"))),
                                     prevent_empty_text=bool(params.get("prevent_empty_text", False)),
                                     llama_template=str(params.get("llama_template", "") or ""))
            return {"conditioning": conds, "count": len(conds),
                    "images_used": len(imgs or [])}
        if op == "vae_decode":
            latent = params.get("latent")
            images = self.vae_decode(
                params.get("model", ""), latent,
                bool(params.get("tiled", False)),
                int(params.get("tile_size", 512) or 512),
                int(params.get("overlap", 64) or 64),
            )
            return {
                "frames": codec.tensor_to_png_frames(images),
                "height": int(images.shape[1]),
                "width": int(images.shape[2]),
                "channels": int(images.shape[3]),
            }
        if op == "clip_vision_encode":
            images = params.get("images")
            if images is None and params.get("frames"):
                images = codec.png_frames_to_tensor(params["frames"])
            return self.clip_vision_encode(params.get("model", ""), images)
        if op == "clip_vision_list":
            return {"dir": self.clip_vision_dir(), "files": self.list_clip_vision_files()}
        if op == "ref_latent_encode":
            pixels = params.get("pixels")
            if pixels is None and params.get("frames"):
                pixels = codec.png_frames_to_tensor(params["frames"])
            return self.ref_latent_encode(
                params.get("model", ""), pixels,
                int(params.get("max_pixels", 1048576) or 0),
                int(params.get("multiple", 8) or 8),
            )
        if op == "vae_encode":
            pixels = params.get("pixels")
            if pixels is None and params.get("frames"):
                pixels = codec.png_frames_to_tensor(params["frames"])
            samples = self.vae_encode(
                params.get("model", ""), pixels,
                bool(params.get("tiled", False)),
                int(params.get("tile_size", 512) or 512),
                int(params.get("overlap", 64) or 64),
            )
            return {"samples": samples}
        # ---------------- LLM（提示词优化：用服务端 ComfyUI 里的 GGUF） ---------------- #
        if op == "gguf_encode":
            conds = self.gguf_encode(
                str(params.get("model") or ""), str(params.get("text") or ""),
                clip_type=str(params.get("clip_type") or ""),
                last_layer=int(params.get("last_layer") or 0),
                images=params.get("images") or [],
                keep_vision=params.get("keep_vision"),
                llama_template=str(params.get("llama_template") or ""),
            )
            return {"conditioning": conds, "count": len(conds), "gguf": params.get("model") or ""}
        if op == "unet_load_gguf":
            # v1.7.64：把 GGUF 当扩散模型加载（预加载 + 可行性探测）
            name = str(params.get("model") or "")
            if not name:
                raise ServerError("unet_load_gguf 缺少 model")
            self.get_unet_gguf(name)
            dev = ""
            for k in ("unet",):
                for n, obj in self._loaded[k].items():
                    if n == "gguf::" + name:
                        dev = str(getattr(obj, "load_device", "") or "")
            return {"loaded": name, "device": dev,
                    "loaded_kinds": {k: list(v.keys()) for k, v in self._loaded.items()}}
        if op == "unet_sample":
            return self.unet_sample(
                str(params.get("model") or ""),
                params.get("latent"),
                params.get("positive") or [],
                params.get("negative") or [],
                steps=int(params.get("steps") or 20),
                cfg=float(params.get("cfg") if params.get("cfg") is not None else 5.0),
                sampler_name=str(params.get("sampler_name") or "euler"),
                scheduler=str(params.get("scheduler") or "normal"),
                seed=int(params.get("seed") or 0),
                denoise=float(params.get("denoise") if params.get("denoise") is not None else 1.0),
            )
        if op == "llm_models":
            return self.llm.list_models(ttl=0.0)
        if op == "llm_status":
            return self.llm.status()
        if op == "llm_env":
            return self.llm.env()
        if op == "llm_uninstall":
            return self.llm.uninstall()
        if op == "llm_bench":
            return self.llm.bench(int(params.get("prompt_tokens", 512) or 512),
                                  int(params.get("gen_tokens", 64) or 64))
        if op == "llm_load":
            return self.llm.load(str(params.get("model") or ""),
                                 str(params.get("mmproj") or ""),
                                 int(params.get("n_ctx", 4096) or 4096),
                                 int(params.get("n_gpu_layers", -1) if params.get("n_gpu_layers") is not None else -1))
        if op == "llm_unload":
            before = self._vram()
            st = self.llm.unload()
            self._hard_free_gpu()
            after = self._vram()
            st["freed"] = ["llm: worker 进程已退出"]
            st["vram_before"] = before
            st["vram_after"] = after
            return st
        if op == "llm_chat":
            images = params.get("images") or []
            if not images and params.get("frames"):
                images = params["frames"]
            data = self.llm.chat(
                str(params.get("model") or ""),
                str(params.get("text") or ""),
                system=str(params.get("system") or ""),
                images=list(images or []),
                max_tokens=int(params.get("max_tokens", 1024) or 1024),
                temperature=float(params.get("temperature", 0.7) or 0.7),
                seed=(None if params.get("seed") is None else int(params.get("seed"))),
                mmproj=str(params.get("mmproj") or ""),
                n_ctx=int(params.get("n_ctx", 4096) or 4096),
                # v1.7.34：timeout 传 None = 不设上限，慢慢生成也等到底
                timeout=(float(params["timeout"]) if params.get("timeout") else None),
                auto_load=bool(params.get("auto_load", True)),
            )
            return data
        raise ServerError(f"未知操作：{op!r}")


class MockBackend(Backend):
    """
    联调用假后端：不加载任何模型，只用随机张量走完整链路。
    用法：python server.py --mock
    作用：先在两台机器上验证网络 / 协议 / 节点接线是否通，再换真模型。
    """

    def __init__(self, cfg: dict, config_dir: str):
        super().__init__(cfg, config_dir)
        self.idle_unload = 0
        from llm import MockLLM
        self.llm = MockLLM(self)

    def _import_comfy(self):
        return {}

    def list_models(self, ttl: float = 4.0):
        return {
            "clip": {"mock_clip_768": {"files": [], "resolved": [], "exists": True,
                                      "clip_type": "stable_diffusion", "note": "SD1.5 形状的假 CLIP"}},
            "vae": {"mock_vae": {"files": [], "resolved": [], "exists": True,
                                "clip_type": None, "note": "8x 下采样的假 VAE"}},
            "clip_vision": {"mock_clipvision.safetensors": {"files": [], "resolved": [],
                                                            "exists": True, "note": "假视觉编码器"}},
            "llm": {"mock_llm.gguf": {"files": ["mock_llm.gguf"], "exists": True, "note": "假 GGUF"}},
            "llm_vision": {"mock_mmproj.gguf": {"files": ["mock_mmproj.gguf"], "exists": True,
                                                "note": "假 mmproj"}},
            "unet_gguf": {"mock_unet.gguf": {"files": ["mock_unet.gguf"], "resolved": ["mock_unet.gguf"],
                                             "exists": True, "note": "假扩散模型 GGUF"}},
        }

    def get_clip(self, name, clip_type=""):
        return object()

    def get_vae(self, name):
        return object()

    def clip_encode(self, model, text, last_layer=0, clip_type="", images=None, vision_pixels=1048576,
                    vae="", resolution=0, no_resize=False, keep_vision=None, prevent_empty_text=False,
                    llama_template=""):
        n = len(images or [])
        ctx = torch.randn(1, 77 + 64 * n, 768, dtype=torch.float32)
        out = [[ctx, {"pooled_output": torch.randn(1, 1280, dtype=torch.float32)}]]
        if vae and n:
            out = self._append_cond_values(out, "reference_latents",
                                           [torch.randn(1, 16, 40, 40) for _ in range(n)])
        return out

    def gguf_encode(self, model, text, clip_type="", last_layer=0, images=None,
                    keep_vision=None, llama_template=""):
        # MOCK：假的 GGUF 文本编码器，形状和真实 Qwen 文本编码器一致（1, 77, 768）
        return [[torch.randn(1, 77, 768, dtype=torch.float32),
                 {"pooled_output": torch.randn(1, 1280, dtype=torch.float32)}]]

    def get_unet_gguf(self, name):
        return object()

    def unet_sample(self, model, latent, positive, negative, steps=20, cfg=5.0,
                    sampler_name="euler", scheduler="normal", seed=0, denoise=1.0):
        lat = latent.get("samples", latent) if isinstance(latent, dict) else latent
        return {"samples": lat, "seconds": 0.1}

    def vae_decode(self, model, samples, tiled=False, tile_size=512, overlap=64):
        b, c, h, w = samples.shape
        return torch.rand(b, h * 8, w * 8, 3, dtype=torch.float32)

    def vae_encode(self, model, pixels, tiled=False, tile_size=512, overlap=64):
        b, h, w, c = pixels.shape
        return torch.randn(b, 4, h // 8, w // 8, dtype=torch.float32)

    def list_clip_vision_files(self):
        return ["mock_clipvision.safetensors"]

    def get_clip_vision(self, name=""):
        return object()

    def clip_vision_encode(self, model, images):
        b = images.shape[0]
        return {"image_embeds": torch.randn(b, 81, 64, dtype=torch.float32),
                "penultimate_hidden_states": torch.randn(b, 729, 64, dtype=torch.float32),
                "last_hidden_state": torch.randn(b, 729, 64, dtype=torch.float32),
                "image_sizes": [], "model": "mock_clipvision.safetensors"}

    def ref_latent_encode(self, model, pixels, max_pixels=1048576, multiple=8):
        pixels = self._fit_pixels(pixels, max_pixels, max(8, multiple))
        return {"samples": self.vae_encode(model, pixels),
                "height": int(pixels.shape[1]), "width": int(pixels.shape[2])}

    def status(self, light: bool = False):
        s = super().status(light=light)
        s["mock"] = True
        s["device_name"] = "MOCK"
        s["models"] = self.list_models()
        return s
