# -*- coding: utf-8 -*-
"""
nodes.py —— ComfyUI 侧的「远程加载」节点。

设计要点：
  * 本机只保存一个极小的"代理对象"（地址 + 模型名 + 超时），绝不加载模型，
    显存全部留给图片生成模型（UNet / DiT）。
  * 加载器输出的是**真正的 CLIP / VAE 类型**（鸭子类型代理），所以可以直连
    ComfyUI 自带的 CLIPTextEncode / VAEDecode / VAEEncode / CLIPSetLastLayer，
    也可以连本套件的「远程 CLIP 文本编码」「远程 VAE 解码 / 编码」。
  * 文本编码结果（CONDITIONING）和 VAE 结果都是纯张量，跨机无损；图像走 PNG。

节点（分类 comfyui_remote_clip_vae）：
  远程 CLIP 加载器 (Remote CLIP Loader)   → clip (类型 CLIP)
  远程 VAE 加载器 (Remote VAE)            → vae  (类型 VAE)
  远程 CLIP 文本编码 (Remote CLIP Encode) → 正向条件 / 负向条件
  远程 VAE 解码 (Remote VAE Decode)       → 图像
  远程 VAE 编码 (Remote VAE Encode)       → latent
"""

from __future__ import annotations

import json
import os
import random
import re
import time

import torch

from . import remote_api
from .codec import png_frames_to_tensor, tensor_to_png_frames
from .settings import default_server

CATEGORY = "comfyui_remote_clip_vae"
_LIST_TTL = 60.0

GPU_HINT = "（点「🔄 刷新模型列表」获取）"
DEV_HINT = "（点「🔄 刷新模型列表」自动识别）"
FAIL_HINT = "（连接失败，检查服务端）"
NO_MODEL = "（没有可用模型，去服务端加目录后重扫）"

CLIP_TYPES = [
    "自动（按文件名判断）",
    "stable_diffusion",
    "sdxl",
    "sd3",
    "flux",
    "qwen_image",
    "hidream",
    "wan",
    "chroma",
    "ltxv",
    "hunyuan_video",
    "stable_cascade",
    "t5",
    "mochi",
]


def _populate_clip_types(base):
    """v1.7.49：clip_type 下拉直接对齐 ComfyUI 自己的 CLIPType 枚举。

    以前是写死的 14 项，ComfyUI 以后新增类型（或你自己加了自定义 clip_type）就选不到。
    这里在导入时动态合并一次；导入不到就退回静态列表，不影响启动。
    """
    out = list(base)
    low = {b.lower() for b in out}
    try:
        from comfy import sd as _sd
        for t in _sd.CLIPType:
            name = str(t.name).lower()
            if name and name not in low:
                out.append(name)
                low.add(name)
    except Exception:
        pass
    return out


CLIP_TYPES = _populate_clip_types(CLIP_TYPES)

_LIST_CACHE: dict = {}
_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_cache.json")


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _load_cache() -> dict:
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cache(key: str, names: list[str]) -> None:
    """把上次成功拿到的清单落盘：ComfyUI 重启时服务端还没开，下拉也是真名字。"""
    try:
        data = _load_cache()
        data[key] = list(names)
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _model_options(kind: str, url: str, token: str) -> tuple[list[str], str]:
    key = (kind, (url or "").strip(), (token or "").strip())
    now = time.time()
    hit = _LIST_CACHE.get(key)
    if hit and now - hit[0] < _LIST_TTL:
        return hit[1], hit[2]
    names, err = remote_api.list_models(url, token, kind)
    if names:
        _save_cache(f"{kind}|{key[1]}", names)
    else:
        disk = _load_cache().get(f"{kind}|{key[1]}") or []
        if isinstance(disk, list) and disk:
            names, err = disk, ""
    _LIST_CACHE[key] = (now, names, err)
    return names, err


def _cfg_token() -> str:
    """token 不再出现在节点上：统一取默认服务端配置（default_server.json / 侧边栏设置）里的 token。"""
    try:
        return str(default_server().get("token") or "")
    except Exception:
        return ""


# ---- 「卸载模型」五档：卸载到内存 / 自动 / 保持加载 / 运行后卸载 / 运行前后卸载 ----
UNLOAD_RAM = "卸载到内存"
UNLOAD_AUTO = "自动"
UNLOAD_KEEP = "保持加载"
UNLOAD_AFTER = "运行后自动卸载"
UNLOAD_BOTH = "运行前后自动卸载"
UNLOAD_MODES = [UNLOAD_RAM, UNLOAD_AUTO, UNLOAD_KEEP, UNLOAD_AFTER, UNLOAD_BOTH]
UNLOAD_DEFAULT = UNLOAD_RAM


def _unload_plan(mode: str, loader: bool = False) -> tuple[bool, bool]:
    """返回 (运行前是否卸载, 运行后是否卸载)。
    loader=True 表示这是「加载器」节点：它的任务就是把模型加载好交给下游，
    所以选「卸载到内存」时不在这里搬走（否则下游马上又要搬回来/重读一遍磁盘）。"""
    m = (mode or UNLOAD_DEFAULT).strip()
    if loader and m == UNLOAD_RAM:
        return False, False
    if m == UNLOAD_BOTH:
        return True, True
    if m in (UNLOAD_AFTER, UNLOAD_RAM):
        return False, True
    return False, False


def _unload_mode(mode: str) -> str:
    """卸载方式：ram = 只把权重从显存搬到内存（进度更快、显存照样还回去）；full = 连对象一起释放。"""
    return "ram" if (mode or "").strip().startswith("卸载到内存") else "full"


def _unload_remote(url: str, token: str, kind: str, model: str = "", why: str = "",
                   mode: str = "full") -> list:
    """让服务端把某个模型踢出显存（kind 留空 = 该类全部）；失败只打印，不打断出图。
    mode="ram" 时服务端只把权重搬到内存并保留缓存，下次用只需搬回显存，不用重读磁盘。"""
    try:
        data = remote_api.rpc(url, token, "unload",
                              {"kind": kind or "", "model": model or "",
                               "mode": mode or "full"}, timeout=None) or {}
        freed = data.get("freed") or []
        va = data.get("vram_after") or {}
        vb = data.get("vram_before") or {}
        vtxt = ""
        if isinstance(va, dict) and va.get("used_gb") is not None:
            vtxt = (f" | 显存 {vb.get('used_gb')}GB → {va.get('used_gb')}GB"
                    if vb.get("used_gb") is not None else f" | 显存 {va.get('used_gb')}GB")
        print(f"[远程卸载] {why or kind}: {kind or '*'}={model or '*'} → "
              f"{'搬到内存' if (mode or 'full') == 'ram' else '释放'} "
              f"{freed or '（本来就没加载）'}{vtxt}", flush=True)
        return freed
    except Exception as e:
        print(f"[远程卸载] {why or kind} 跳过：{e}", flush=True)
        return []


def _llm_unload_remote(url: str, token: str, why: str = "", mode: str = "full") -> list:
    """GGUF（llama.cpp）那边的「卸载」。
    mode="ram" → 调用 llm_offload_ram：把同一支 GGUF 以「0 层上卡」重载，权重留内存、显存还回去；
    下次节点再跑时按它自己的「上卡层数」把权重搬回显存（不用重新算 tokenizer）。
    mode="full" → llm_unload：整个 worker 侧卸载，显存和内存都放掉。"""
    op = "llm_offload_ram" if (mode or "full") == "ram" else "llm_unload"
    try:
        data = remote_api.rpc(url, token, op, {}, timeout=None) or {}
        note = str(data.get("note") or "")
        moved = data.get("moved") or []
        print(f"[远程卸载] {why or 'GGUF'} → {'搬到内存' if op == 'llm_offload_ram' else '卸载'} "
              f"{moved or '（没有已加载的 GGUF）'}{(' | ' + note) if note else ''}", flush=True)
        return moved
    except Exception as e:
        print(f"[远程卸载] {why or 'GGUF'} 跳过：{e}", flush=True)
        return []


# ---- 提示词优化的「本地模式」：直接在本机 ComfyUI 的 python 里跑 llama-cpp ----
_LOCAL_LLM: dict = {}
LOCAL_GGUF_SUBS = ("LLM", "llm", "llm_gguf", "LLM_gguf", "gguf", "text_encoders_llm")


def _local_gguf_dirs() -> list:
    try:
        import folder_paths

        root = folder_paths.models_dir
    except Exception:
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")
    dirs = []
    for sub in LOCAL_GGUF_SUBS:
        p = os.path.join(root, sub)
        if os.path.isdir(p) and p not in dirs:
            dirs.append(p)
    return dirs


def _local_gguf_scan() -> tuple[list, list]:
    """扫本机 models/LLM 一类的目录：返回 (文本 GGUF, mmproj 视觉投影)。"""
    names, visions = [], []
    for d in _local_gguf_dirs():
        for dp, _dn, fs in os.walk(d):
            for f in fs:
                if not f.lower().endswith(".gguf"):
                    continue
                rel = os.path.relpath(os.path.join(dp, f), d).replace("\\", "/")
                low = f.lower()
                if any(k in low for k in ("mmproj", "vision", "vit")):
                    visions.append(rel)
                else:
                    names.append(rel)
    return sorted(set(names)), sorted(set(visions))


def _local_gguf_path(name: str) -> str:
    """把下拉里的相对名解析成本机绝对路径；绝对路径也直接认。"""
    n = str(name or "").strip()
    if not n or n.startswith("（") or n.startswith("("):
        return ""
    if os.path.isabs(n) and os.path.exists(n):
        return n
    for d in _local_gguf_dirs():
        p = os.path.join(d, n.replace("/", os.sep))
        if os.path.exists(p):
            return p
    base = os.path.basename(n)
    for d in _local_gguf_dirs():
        for dp, _dn, fs in os.walk(d):
            if base in fs:
                return os.path.join(dp, base)
    return ""


def _local_llm_free():
    for k, obj in list(_LOCAL_LLM.items()):
        try:
            del obj
        except Exception:
            pass
        _LOCAL_LLM.pop(k, None)
    try:
        import gc

        gc.collect()
    except Exception:
        pass
    try:
        torch.cuda.empty_cache()
    except Exception:
        pass


def _local_llm_get(path: str, mmproj: str, n_ctx: int):
    """按 (模型, mmproj, n_ctx) 缓存一个本地 Llama 实例；换模型就先把旧的放掉。"""
    key = (path, mmproj, int(n_ctx))
    hit = _LOCAL_LLM.get(key)
    if hit is not None:
        return hit
    try:
        from llama_cpp import Llama
    except Exception as e:
        raise RuntimeError(
            "本机没有 llama-cpp-python，用不了本地模式。\n"
            "二选一：① 在本机 ComfyUI 的 python 里装 `llama-cpp-python`（Windows 上建议用 "
            "JamePeng/llama-cpp-python 的预编译 wheel，和 CUDA 版本对齐）；\n"
            f"② 把开关「使用远程模型」打开，改用服务端那台机器上的 GGUF。\n原始错误：{e}"
        ) from None
    _local_llm_free()
    kw = dict(model_path=path, n_ctx=int(n_ctx), n_gpu_layers=-1, verbose=False)
    llm = Llama(**kw)
    handler_name = ""
    if mmproj:
        handler = None
        for cls_name in ("Qwen25VLChatHandler", "Qwen2VLChatHandler", "Llava15ChatHandler", "LlavaChatHandler"):
            try:
                mod = __import__("llama_cpp.llama_chat_format", fromlist=[cls_name])
                handler = getattr(mod, cls_name)(clip_model_path=mmproj, verbose=False)
                handler_name = cls_name
                break
            except Exception:
                handler = None
                continue
        if handler is None:
            raise RuntimeError("本机这份 llama-cpp-python 不支持多模态 chat handler，带图请把开关打开走服务端。")
        llm.chat_handler = handler
    _LOCAL_LLM[key] = llm
    print(f"[提示词优化-本地] 已加载本地 GGUF：{os.path.basename(path)} | mmproj={os.path.basename(mmproj) or '无'}"
          f"{' | handler=' + handler_name if handler_name else ''} | n_ctx={n_ctx}")
    return llm


def _combo(kind: str, url: str, token: str) -> tuple[list, dict]:
    names, err = _model_options(kind, url, token)
    if not names:
        msg = FAIL_HINT if err else NO_MODEL
        return [msg], {"default": msg}
    return names, {"default": names[0]}


def _pick_model(model: str, kind: str) -> str:
    """模型只从「模型」下拉里选（「手动模型名」已彻底删掉）；没选就报清楚的中文错误。"""
    name = (model or "").strip()
    if not name or name.startswith("（") or name.startswith("("):
        raise ValueError(
            f"没有可用的 {kind.upper()} 模型：先在服务端界面选好模型目录并扫描，"
            f"再点节点上的「🔄 刷新模型列表」，在「模型」下拉里选一个。"
        )
    return name


def _clip_type_value(v: str) -> str:
    """自动 = 让服务端按 config.json 里的 clip_type 走。
    兼容旧标签写法，如 "flux（Flux 的 t5xxl / clip_l）" → "flux"。"""
    v = (v or "").strip()
    if not v or v.startswith("自动"):
        return ""
    for sep in ("（", "("):
        if sep in v:
            v = v.split(sep)[0]
    return v.strip()


def _norm_conditioning(conds) -> list:
    """统一成 ComfyUI 的 CONDITIONING：[[tensor, dict], ...]"""
    out = []
    for item in conds or []:
        if torch.is_tensor(item):
            out.append([item, {}])
        elif isinstance(item, dict):
            d = dict(item)
            cond = d.pop("cond", None)
            if cond is None:
                raise RuntimeError("服务端返回的 conditioning 结构无法识别")
            out.append([cond, d])
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            out.append([item[0], dict(item[1] or {})])
        else:
            raise RuntimeError(f"服务端返回的 conditioning 结构无法识别：{type(item)!r}")
    if not out:
        raise RuntimeError("服务端返回的 conditioning 为空")
    return out


def _target_device(pref: str):
    """pref: cpu / gpu / auto。auto = 与原生节点一致（模型设备）。"""
    if pref == "cpu":
        return None
    try:
        import comfy.model_management as mm
        return mm.get_torch_device()
    except Exception:
        return None


def _to_device(t, pref: str):
    if isinstance(t, list):
        return [_to_device(v, pref) for v in t]
    if isinstance(t, tuple):
        return tuple(_to_device(v, pref) for v in t)
    if isinstance(t, dict):
        return {k: _to_device(v, pref) for k, v in t.items()}
    if not torch.is_tensor(t):
        return t
    dev = _target_device(pref)
    return t if dev is None else t.to(dev)


def _norm_url(u: str) -> str:
    """把服务端地址规范化成「主机:端口」，用于比较是不是同一台服务端。
    放过 http:// 前缀、末尾斜杠、大小写、:80/:443 这类纯格式差异。"""
    s = (u or "").strip()
    s = s.rstrip("/").lower()
    for p in ("https://", "http://"):
        if s.startswith(p):
            s = s[len(p):]
            break
    for p in (":443", ":80"):
        if s.endswith(p):
            s = s[: -len(p)]
    return s


def _check_url(url: str):
    if not url or url.strip().startswith("<") or url.strip().startswith("（"):
        raise ValueError("请先填写「服务端地址」，形如 http://192.168.1.20:8199")


def _validate_model(name: str) -> bool | str:
    """v1.7.49：不再在队列校验阶段拦模型名。

    说明：模型清单是运行时从服务端拉的，ComfyUI 启动时固化的那份必然过时；
    以前这里返回字符串会在画布上弹「无效输入」，换个模型就被拦一下，很碍事。
    现在一律放行，占位符/空值在执行阶段用中文报错（见 _require_model）。
    """
    return True


def _require_model(name: str, kind: str = "CLIP") -> str:
    """执行阶段检查：模型名是不是占位符/空的。返回可用的名字。"""
    v = (name or "").strip()
    if not v or v.startswith("（") or v.startswith("(") or v.startswith("<"):
        raise ValueError(
            f"「模型」还没选：点节点上的「🔄 刷新模型列表」选一个服务端已有的{kind}条目"
            f"（模型清单是运行时从服务端拉的；「手动模型名」已取消）。"
        )
    return v


def _handle_of(obj):
    """老工作流里连过来的是 v1.7.3 的字典句柄，也兼容。"""
    return obj if isinstance(obj, dict) else None


def _vae_ref_latents(vae, imgs, px: int = 0):
    """用【本机原生 VAE】把参考图编成 reference_latents。

    v1.7.53：尺寸由「参考图最大像素」决定（和视觉 token / 服务端那条路完全同一套算法），
    这样本机 VAE 与远程 VAE 两条路产出的 latent 尺寸一致 —— 接本机 VAE 变糊的问题就没了。
    px<=0 时退回原图尺寸。
    """
    import math

    if vae is None or not imgs:
        return []
    enc = getattr(vae, "encode", None)
    if not callable(enc):
        print("[远程CLIP图文编码] ⚠ 接进来的 vae 没有 encode()，没法在本机编 reference_latents；"
              "请接「VAE 加载器」（原生）或「远程 VAE 加载器」。", flush=True)
        return []
    try:
        from comfy.utils import common_upscale as _upscale
    except Exception:  # 不在 ComfyUI 环境里直接跳过缩放
        _upscale = None

    lats = []
    for idx, img in enumerate(imgs, 1):
        t = img if torch.is_tensor(img) else torch.as_tensor(img)
        if t.dim() == 3:
            t = t.unsqueeze(0)
        s = t[:1].float().movedim(-1, 1)  # B,C,H,W
        src_w, src_h = int(s.shape[3]), int(s.shape[2])
        # 按 VAE 自己的空间缩放比取整（不猜 8，读不到就退回 8）
        try:
            ds = int(getattr(vae, "spacial_compression_encode", lambda: 8)() or 8)
        except Exception:
            ds = 8
        if px and px > 0:
            res = int(round(float(px) ** 0.5))
            ratio = src_w / src_h
            width = max(64, int(round(math.sqrt(res * res * ratio) / 32) * 32))
            height = max(64, int(round(math.sqrt(res * res / ratio) / 32) * 32))
        else:
            # px=0：原图尺寸，只把宽高取整到 VAE 缩放比的整数倍（最多掉几像素，不放大不缩小）
            width = max(64, (src_w // ds) * ds)
            height = max(64, (src_h // ds) * ds)
        if (width, height) != (src_w, src_h):
            if _upscale is not None:
                s = _upscale(s, width, height, "lanczos", "disabled")
            else:
                s = torch.nn.functional.interpolate(s, size=(height, width), mode="bicubic",
                                                    align_corners=False)
        s = s.movedim(1, -1)[:, :, :, :3]  # BHWC（VAE 不吃 alpha）
        lat = enc(s)
        # v1.7.49：这里只做**提示**，绝不拦。通道数/缩放比都从你实际接的 VAE 读出来。
        try:
            ch, lh, lw = int(lat.shape[1]), int(lat.shape[-2]), int(lat.shape[-1])
            ds_h = height // lh if lh else 0
            ds_w = width // lw if lw else 0
            if ds_h != ds_w or ds_h < 1:
                print(f"[远程CLIP图文编码] 提示：本机 vae 的缩放比不是正方形（{width}×{height} → "
                      f"{lw}×{lh}），如果出图比例异常，检查 VAE 是否与模型配套。", flush=True)
        except Exception:
            pass
        print(f"[远程CLIP图文编码] 本机 vae 编码第 {idx} 张参考图：{src_w}×{src_h} → "
              f"{width}×{height} → latent {tuple(lat.shape)}", flush=True)
        # v1.7.51：不再用固定阈值判断 latent 统计 —— 不同 VAE 的尺度本来就不一样
        # （qwen_image 2.1 的 64 通道 VAE 实测 std≈4.2、范围 ±20，完全正常）。
        try:
            _l = lat.detach().float()
            print(f"[远程CLIP图文编码]   该 latent 统计：std={_l.std():.3f} "
                  f"min={_l.min():.2f} max={_l.max():.2f}（不同 VAE 尺度不同，仅供对比）", flush=True)
        except Exception:
            pass
        # v1.7.53：token 数只作参考（1MP 档约 4000~4200 patch 最稳）
        try:
            toks = int(lat.shape[-2]) * int(lat.shape[-1])
            if toks > 6000:
                print(f"[远程CLIP图文编码] 提示：参考 latent {int(lat.shape[-2])}×{int(lat.shape[-1])}"
                      f" = {toks} 个 patch（1MP 一档约 4000~4200）。这张图很大，"
                      f"把「参考图最大像素」调到 1.0 左右更稳。", flush=True)
        except Exception:
            pass
        lats.append(lat)
    return lats


def _attach_ref(conds, lats):
    """把 reference_latents 追加到 CONDITIONING 的每个 [tensor, dict] 里（等价 append=True）。"""
    if not lats:
        return conds
    out = []
    for c, d in conds:
        d = dict(d or {})
        d["reference_latents"] = list(d.get("reference_latents") or []) + list(lats)
        out.append([c, d])
    return out


def _connect_error(url: str, e) -> RuntimeError:
    return RuntimeError(
        f"连不上服务端 {url}：{e}\n"
        f"排查：① 服务端程序是否在运行；② 两台机器是否同一局域网；"
        f"③ 服务端防火墙是否放行该端口；④ 地址里的 IP/端口是否填对"
    )


# --------------------------------------------------------------------------- #
# 图片接口（动态增槽）
# --------------------------------------------------------------------------- #
IMAGE_SLOTS = 10

MODE_TEXT = "关闭（只编码文字）"
MODE_VL = "图片 → 服务端文本编码器（Qwen-VL 图文混编，Qwen-Image 编辑用）"
MODE_LATENT = "参考图 → 服务端 VAE 编码（Qwen/Flux Edit 式）"
MODE_VISION = "图片 → 服务端 CLIP 视觉（Redux 式）"
IMAGE_MODES = [MODE_TEXT, MODE_VL, MODE_LATENT, MODE_VISION]


def _collect_images(kw: dict) -> list:
    """把 图片1..图片10 里连上的图按顺序收起来。"""
    out = []
    for i in range(1, IMAGE_SLOTS + 1):
        v = kw.get(f"图片{i}")
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            out.extend([x for x in v if x is not None])
        else:
            out.append(v)
    return out


def _apply_extra(conds: list, extra: dict) -> list:
    """给 conditioning 挂附加信息（reference_latents / clip_vision_output）。"""
    try:
        import node_helpers  # ComfyUI 自带
        for k, v in extra.items():
            conds = node_helpers.conditioning_set_values(conds, {k: v}, append=False)
        return conds
    except Exception:
        out = []
        for c, d in conds:
            d2 = dict(d)
            d2.update(extra)
            out.append([c, d2])
        return out


class _VisionOut:
    """clip_vision 输出的鸭子类型替身（有 comfy 时用官方 Output 类）。"""

    def __getitem__(self, key):
        return getattr(self, key)

    def __setitem__(self, key, item):
        setattr(self, key, item)


def _make_vision_output(payload: dict) -> object:
    try:
        from comfy.clip_vision import Output as _Out
        out = _Out()
    except Exception:
        out = _VisionOut()
    for k in ("image_embeds", "penultimate_hidden_states", "last_hidden_state"):
        v = payload.get(k)
        if torch.is_tensor(v):
            out[k] = v
    out["image_sizes"] = payload.get("image_sizes") or []
    return out


def _cat_vision_outputs(parts: list) -> object:
    out = _make_vision_output({})
    for k in ("image_embeds", "penultimate_hidden_states", "last_hidden_state"):
        vals = [p[k] for p in parts if torch.is_tensor(getattr(p, k, None)) and getattr(p, k).shape[0] > 0]
        if not vals:
            continue
        try:
            out[k] = torch.cat(vals, dim=0)
        except Exception:
            out[k] = vals[0]
    sizes = []
    for p in parts:
        sizes.extend(list(getattr(p, "image_sizes", []) or []))
    out["image_sizes"] = sizes
    return out


# --------------------------------------------------------------------------- #
# 远程 CLIP 代理（鸭子类型：可直接接原生 CLIPTextEncode / CLIPSetLastLayer）
# --------------------------------------------------------------------------- #
class RemoteCLIP:
    def __init__(self, url: str, token: str, model: str, clip_type: str = "",
                 timeout: float | None = None, last_layer: int = 0):
        self.remote = True
        self.server_url = url
        self.token = token or ""
        self.model = model
        self.clip_type = clip_type or ""
        # v1.7.34：不再有任何超时，None = 想等多久就等多久（大 CLIP 首次加载/大图编码都能跑完）
        self.timeout = None
        self.last_layer = int(last_layer or 0)
        # 原生代码偶尔会摸这几个属性
        self.device = torch.device("cpu")
        self.layer_idx = None
        self.patcher = None
        self.cond_stage_model = None
        self.use_clip_schedule = False
        self.options = {}
        self._img_cache = {}

    def __repr__(self) -> str:
        return f"<RemoteCLIP model={self.model!r} server={self.server_url!r} layer={self.last_layer}>"

    # ---- 原生节点用到的接口 --------------------------------------------- #
    def tokenize(self, text, return_word_ids=False, images=None, keep_vision=None,
                 prevent_empty_text=False, llama_template=None, **kwargs):
        """原生节点（如「文本编码 Qwen Image 编辑 Plus」）是这样调的：
        clip.tokenize(prompt, images=images_vl, keep_vision=..., prevent_empty_text=True)

        真正的分词在服务端做，所以这里必须把 images / keep_vision 一起带过去；
        否则图片会被悄悄丢掉（图片没进 vision 槽 → 参考 latent 插错位置 → 出图糊）。
        """
        payload = {"text": text if isinstance(text, str) else str(text),
                   "keep_vision": None if keep_vision is None else bool(keep_vision),
                   "prevent_empty_text": bool(prevent_empty_text),
                   "llama_template": llama_template if isinstance(llama_template, str) else ""}
        if images:
            payload["images"] = self._pack_images(images)
        # 文本放两份：encode_from_tokens_scheduled 直接读 __remote_clip_text__，
        # 早先只塞在内层 payload 里、_text_of 又只查顶层，导致服务端收到空文本（出图糊/重影）
        return {"__remote_clip_text__": payload["text"], "__remote_clip__": payload}

    def _pack_images(self, images) -> list:
        groups = []
        for img in images:
            if img is None:
                continue
            if not torch.is_tensor(img):
                img = torch.as_tensor(img)
            key = (id(img), tuple(img.shape))
            hit = self._img_cache.get(key)
            if hit is None:
                t = img
                if t.dim() == 3:
                    t = t.unsqueeze(0)
                if t.shape[-1] > 3:
                    t = t[..., :3]
                hit = tensor_to_png_frames(t.detach().float().cpu())
                if len(self._img_cache) > 8:
                    self._img_cache.clear()
                self._img_cache[key] = hit
            groups.append(hit)
        return groups

    @staticmethod
    def _text_of(tokens) -> str:
        if isinstance(tokens, dict):
            if "__remote_clip_text__" in tokens:
                return tokens["__remote_clip_text__"]
            inner = tokens.get("__remote_clip__")
            if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                return inner["text"]
            if "text" in tokens:
                return tokens["text"]
            for v in tokens.values():  # 兼容原生 tokenize 产出的结构
                if isinstance(v, str):
                    return v
                if isinstance(v, (list, tuple)) and v and isinstance(v[0], str):
                    return " ".join(str(x) for x in v)
            return ""
        return tokens if isinstance(tokens, str) else str(tokens)

    def encode_from_tokens_scheduled(self, tokens, opts=None):
        text = self._text_of(tokens)
        payload = {"model": self.model, "text": text, "clip_type": self.clip_type,
                   "last_layer": self.last_layer}
        if isinstance(tokens, dict) and "__remote_clip__" in tokens:
            p = tokens["__remote_clip__"]
            if p.get("images"):
                # 原生节点已经把图缩放到 32 的倍数，服务端不要再缩一次
                payload["images"] = p["images"]
                payload["no_resize"] = True
                payload["prevent_empty_text"] = bool(p.get("prevent_empty_text"))
                if p.get("llama_template"):
                    payload["llama_template"] = p["llama_template"]
                if p.get("keep_vision") is not None:
                    payload["keep_vision"] = bool(p["keep_vision"])
        try:
            data = remote_api.rpc(
                self.server_url, self.token, "clip_encode", payload,
                timeout=self.timeout,
            )
        except RuntimeError:
            raise
        except Exception as e:
            raise _connect_error(self.server_url, e) from None
        return _norm_conditioning((data or {}).get("conditioning"))

    def encode_from_tokens(self, tokens, return_pooled=False, return_dict=False):
        conds = self.encode_from_tokens_scheduled(tokens)
        cond, extra = conds[0]
        pooled = extra.get("pooled_output")
        if return_dict:
            d = {"cond": cond}
            d.update(extra)
            return d
        if return_pooled:
            return cond, pooled
        return cond

    def encode(self, text, **kwargs):
        return self.encode_from_tokens_scheduled(self.tokenize(text))

    def clip_layer(self, layer_idx: int):
        # 与原生一致：原地改 + 返回自己（CLIPSetLastLayer 就是这么用的）
        self.last_layer = int(layer_idx)
        self.layer_idx = int(layer_idx)
        return self

    def clone(self):
        return RemoteCLIP(self.server_url, self.token, self.model, self.clip_type,
                          self.timeout, self.last_layer)

    def encode_with_images(self, text: str, images: list, vision_pixels: int = 1048576,
                           vae_model: str = "", keep_vision=None):
        """图文混编：把参考图交给服务端文本编码器（Qwen-VL 系支持 images= 的 vision token）。
        给了 vae_model 时服务端会同时用 VAE 编 reference_latents（官方 Qwen-Image-Edit 的做法）。

        v1.7.53 关键修正：keep_vision 不能只按「有没有传 vae_model」推断。
        本机 VAE 编 latent 时 vae_model 是空的，旧逻辑就会 keep_vision=True →
        vision token 与 reference_latents **双重插入** → 出图糊/重影（接远程 VAE 正常、接本机 VAE 糊）。
        现在由调用方显式传 False。"""
        groups = []
        for img in images:
            t = img if torch.is_tensor(img) else torch.as_tensor(img)
            if t.dim() == 3:
                t = t.unsqueeze(0)
            # 不在这里砍 alpha：服务端 _qwen_vl_conditioning 会「视觉塔按白底合成、VAE 保留 alpha」
            groups.append(tensor_to_png_frames(t.detach().float().cpu()))
        px = int(vision_pixels or 0)
        res = int(round(px ** 0.5)) if px > 0 else 0
        try:
            data = remote_api.rpc(
                self.server_url, self.token, "clip_encode",
                {"model": self.model, "text": text, "clip_type": self.clip_type,
                 "last_layer": self.last_layer, "images": groups,
                 "vision_pixels": px, "vae": vae_model or "", "resolution": res,
                 # 有 reference_latents（无论谁编的）都必须 keep_vision=False，
                 # 否则 vision token 与 latent 双重插入（出图糊/重影）
                 "keep_vision": bool(keep_vision) if keep_vision is not None else (not bool(vae_model))},
                timeout=self.timeout,
            )
        except RuntimeError:
            raise
        except Exception as e:
            raise _connect_error(self.server_url, e) from None
        return _norm_conditioning((data or {}).get("conditioning"))

    def set_clip_options(self, options: dict):
        self.options = dict(options or {})

    def get_clip_options(self):
        return dict(self.options)


# --------------------------------------------------------------------------- #
# 远程 VAE 代理（鸭子类型：可直接接原生 VAEDecode / VAEEncode / VAEDecodeTiled）
# --------------------------------------------------------------------------- #
class RemoteVAE:
    def __init__(self, url: str, token: str, model: str, timeout: float | None = None):
        self.remote = True
        self.server_url = url
        self.token = token or ""
        self.model = model
        # v1.7.34：无超时限制
        self.timeout = None
        # 原生代码会读的 VAE 属性，先给默认值；第一次 decode 时向服务端问真实值
        self.downscale_ratio = 8
        self.upscale_ratio = 8
        self.spatial_compression_ratio = 8
        self.latent_channels = 4
        self.output_channels = 3
        self.latent_dim = 2
        self.device = torch.device("cpu")
        self.vae_dtype = torch.float32
        self.first_stage_model = None
        self.memory_used_decode = lambda *a, **k: 0
        self.memory_used_encode = lambda *a, **k: 0
        self.server_info: dict = {}

    def __repr__(self) -> str:
        return f"<RemoteVAE model={self.model!r} server={self.server_url!r}>"

    # ---- 内部 ---------------------------------------------------------- #
    def _info(self) -> dict:
        try:
            data = remote_api.rpc(self.server_url, self.token, "vae_info",
                                  {"model": self.model}, timeout=None)
        except Exception:
            return {}
        if isinstance(data, dict):
            self.server_info = dict(data)
            for key in ("latent_channels", "downscale_ratio", "upscale_ratio"):
                val = data.get(key)
                if isinstance(val, int) and val > 0:
                    setattr(self, key, val)
                    if key == "downscale_ratio":
                        self.spatial_compression_ratio = val
        return self.server_info

    # ---- 原生接口 ------------------------------------------------------- #
    def decode(self, samples_in):
        _ = self._info()
        t = samples_in if torch.is_tensor(samples_in) else torch.as_tensor(samples_in)
        data = remote_api.rpc(
            self.server_url, self.token, "vae_decode",
            {"model": self.model, "latent": t.detach().float().cpu()},
            timeout=self.timeout,
        )
        return png_frames_to_tensor(data["frames"], channels=int(data.get("channels") or 3))

    def encode(self, pixels):
        _ = self._info()
        t = pixels if torch.is_tensor(pixels) else torch.as_tensor(pixels)
        frames = tensor_to_png_frames(t.detach().float().cpu())
        data = remote_api.rpc(
            self.server_url, self.token, "vae_encode",
            {"model": self.model, "frames": frames},
            timeout=self.timeout,
        )
        latent = data["samples"]
        return latent if torch.is_tensor(latent) else torch.as_tensor(latent)

    def encode_ref(self, image, max_pixels: int = 1048576):
        """参考图 → 服务端 VAE latent（Qwen/Flux Edit 的 reference_latents 用）。"""
        _ = self._info()
        t = image if torch.is_tensor(image) else torch.as_tensor(image)
        if t.dim() == 3:
            t = t.unsqueeze(0)
        if t.shape[-1] > 3:
            t = t[..., :3]
        frames = tensor_to_png_frames(t.detach().float().cpu())
        try:
            data = remote_api.rpc(
                self.server_url, self.token, "ref_latent_encode",
                {"model": self.model, "frames": frames,
                 "max_pixels": int(max_pixels or 0), "multiple": 8},
                timeout=self.timeout,
            )
        except RuntimeError:
            raise
        except Exception as e:
            raise _connect_error(self.server_url, e) from None
        latent = data.get("samples") if isinstance(data, dict) else data
        if not torch.is_tensor(latent):
            raise RuntimeError("服务端没有返回参考图 latent（ref_latent_encode）")
        return latent

    def decode_tiled(self, samples, tile_x=None, tile_y=None, overlap=None, **kwargs):
        _ = self._info()
        t = samples if torch.is_tensor(samples) else torch.as_tensor(samples)
        data = remote_api.rpc(
            self.server_url, self.token, "vae_decode",
            {"model": self.model, "latent": t.detach().float().cpu(),
             "tiled": True, "tile_size": int(tile_x or 512), "overlap": int(overlap or 64)},
            timeout=self.timeout,
        )
        return png_frames_to_tensor(data["frames"], channels=int(data.get("channels") or 3))

    def encode_tiled(self, pixels, tile_x=None, tile_y=None, overlap=None, **kwargs):
        _ = self._info()
        t = pixels if torch.is_tensor(pixels) else torch.as_tensor(pixels)
        frames = tensor_to_png_frames(t.detach().float().cpu())
        data = remote_api.rpc(
            self.server_url, self.token, "vae_encode",
            {"model": self.model, "frames": frames,
             "tiled": True, "tile_size": int(tile_x or 512), "overlap": int(overlap or 64)},
            timeout=self.timeout,
        )
        latent = data["samples"]
        return latent if torch.is_tensor(latent) else torch.as_tensor(latent)

    def throw_exception_if_invalid(self):
        self._info()
        return None

    # 原生 VAEDecodeTiled 等节点会问这几个（视频 VAE 才有时间维压缩，返回 None = 不压缩）
    def temporal_compression_decode(self):
        return None

    def temporal_compression_encode(self):
        return None

    # 注意 ComfyUI 源码里就是拼成 spacial_ 的，照抄才不会 AttributeError
    def spacial_compression_decode(self):
        self._info()
        return int(self.downscale_ratio or 8)

    def spacial_compression_encode(self):
        self._info()
        return int(self.downscale_ratio or 8)

    def process_input(self, image):
        return image

    def process_output(self, image):
        return image

    # 有些节点会问"这个 vae 有多大 / 什么 dtype"
    def get_vaes(self):
        return [self]


# --------------------------------------------------------------------------- #
# 1. 远程 CLIP 加载器
# --------------------------------------------------------------------------- #
class RemoteCLIPLoader:
    @classmethod
    def INPUT_TYPES(cls):
        cfg = default_server()
        models, model_opt = _combo("clip", cfg["url"], cfg["token"])
        return {
            "required": {
                "服务端显卡": ("STRING", {"default": GPU_HINT,
                                        "tooltip": "服务端显卡型号，点「🔄 刷新模型列表」自动填，只读"}),
                "计算设备": ("STRING", {"default": DEV_HINT,
                                     "tooltip": "远端实际在用什么算（显卡 / CPU），点刷新自动识别，只读，用户不用选"}),
                "服务端地址": ("STRING", {"default": cfg["url"],
                                        "tooltip": "另一台机器（跑 CLIP/VAE 那台）的地址，含端口"}),
                "模型": (models, {"default": model_opt["default"],
                                "tooltip": "服务端已配置的 CLIP 条目名"}),
                "模型类型": (CLIP_TYPES, {"default": "自动（按文件名判断）",
                                        "tooltip": "自动 = 用服务端的 clip_type；Flux/SD3 等对不上时可手动指定"}),
                "卸载模型": (UNLOAD_MODES, {"default": UNLOAD_RAM,
                                        "tooltip": "自动=交给服务端按需管理；保持加载=不自动卸载；"
                                                   "运行后/运行前后自动卸载=服务端显存紧时用"}),
            },
        }

    RETURN_TYPES = ("CLIP",)
    RETURN_NAMES = ("clip",)
    FUNCTION = "load"
    CATEGORY = CATEGORY
    DESCRIPTION = ("远程 CLIP（类型就是 CLIP，可直接接自带的 CLIPTextEncode / CLIPSetLastLayer）。"
                   "文本编码在服务端执行，本机不占显存。")

    @classmethod
    def VALIDATE_INPUTS(cls, 模型=None, 模型类型=None):
        if 模型 is None:
            return True
        return _validate_model(模型)

    def load(self, **kw):
        url = kw.get("服务端地址", "")
        _check_url(url)
        # 「手动模型名」已彻底删除（老工作流里残留的这个值直接忽略）
        model = _pick_model(kw.get("模型", ""), "clip")
        token = _cfg_token()
        before, after = _unload_plan(kw.get("卸载模型"), loader=True)
        if before:
            _unload_remote(url, token, "clip", model, "CLIP 加载器(运行前)", _unload_mode(kw.get("卸载模型")))
        try:
            remote_api.health(url, token, timeout=None)
        except Exception as e:
            raise _connect_error(url, e) from None
        clip = RemoteCLIP(url, token, model, _clip_type_value(kw.get("模型类型", "")))
        print(f"[远程CLIP] 句柄就绪：{url} | 模型 {model}"
              f"{' | clip_type=' + clip.clip_type if clip.clip_type else ''}")
        if after:
            _unload_remote(url, token, "clip", model, "CLIP 加载器(运行后)", _unload_mode(kw.get("卸载模型")))
        return (clip,)


# --------------------------------------------------------------------------- #
# 2. 远程 VAE 加载器
# --------------------------------------------------------------------------- #
class RemoteVAELoader:
    @classmethod
    def INPUT_TYPES(cls):
        cfg = default_server()
        models, model_opt = _combo("vae", cfg["url"], cfg["token"])
        return {
            "required": {
                "服务端显卡": ("STRING", {"default": GPU_HINT}),
                "计算设备": ("STRING", {"default": DEV_HINT,
                                     "tooltip": "远端实际在用什么算（显卡 / CPU），点刷新自动识别，只读"}),
                "服务端地址": ("STRING", {"default": cfg["url"]}),
                "模型": (models, {"default": model_opt["default"]}),
                "卸载模型": (UNLOAD_MODES, {"default": UNLOAD_RAM}),
            },
        }

    RETURN_TYPES = ("VAE",)
    RETURN_NAMES = ("vae",)
    FUNCTION = "load"
    CATEGORY = CATEGORY
    DESCRIPTION = ("远程 VAE（类型就是 VAE，可直接接自带的 VAEDecode / VAEEncode / VAEDecodeTiled）。"
                   "编解码在服务端执行。")

    @classmethod
    def VALIDATE_INPUTS(cls, 模型=None):
        if 模型 is None:
            return True
        return _validate_model(模型)

    def load(self, **kw):
        url = kw.get("服务端地址", "")
        _check_url(url)
        # 「手动模型名」已彻底删除（老工作流里残留的这个值直接忽略）
        model = _pick_model(kw.get("模型", ""), "vae")
        token = _cfg_token()
        before, after = _unload_plan(kw.get("卸载模型"), loader=True)
        if before:
            _unload_remote(url, token, "vae", model, "VAE 加载器(运行前)", _unload_mode(kw.get("卸载模型")))
        try:
            remote_api.health(url, token, timeout=None)
        except Exception as e:
            raise _connect_error(url, e) from None
        vae = RemoteVAE(url, token, model)
        print(f"[远程VAE] 句柄就绪：{url} | 模型 {model}")
        if after:
            _unload_remote(url, token, "vae", model, "VAE 加载器(运行后)", _unload_mode(kw.get("卸载模型")))
        return (vae,)


# --------------------------------------------------------------------------- #
# 3. 远程 CLIP 文本编码（正向 + 负向一次搞定）
# --------------------------------------------------------------------------- #
class RemoteCLIPTextEncode:
    @classmethod
    def INPUT_TYPES(cls):
        cfg = default_server()
        models, model_opt = _combo("clip", cfg["url"], cfg["token"])
        out = {
            "required": {
                "正向提示词": ("STRING", {"multiline": True, "dynamicPrompts": True, "default": ""}),
                "负向提示词": ("STRING", {"multiline": True, "dynamicPrompts": True, "default": ""}),
                "服务端显卡": ("STRING", {"default": GPU_HINT}),
                "计算设备": ("STRING", {"default": DEV_HINT,
                                     "tooltip": "远端实际在用什么算（显卡 / CPU），点刷新自动识别，只读"}),
                "服务端地址": ("STRING", {"default": cfg["url"]}),
                "模型": (models, {"default": model_opt["default"]}),
                "模型类型": (CLIP_TYPES, {"default": "自动（按文件名判断）"}),
                "卸载模型": (UNLOAD_MODES, {"default": UNLOAD_RAM,
                                        "tooltip": "自动=交给服务端；保持加载=跑完不卸；"
                                                   "运行后/运行前后自动卸载=编码完就把 CLIP 踢出服务端显存"}),
            },
            "optional": {
                "clip": ("CLIP", {"tooltip": "可选：接「远程 CLIP 加载器」的 clip，用它带的地址/模型；不接就用本节点自己的字段"}),
            },
        }
        return out

    RETURN_TYPES = ("CONDITIONING", "CONDITIONING")
    RETURN_NAMES = ("正向条件", "负向条件")
    FUNCTION = "encode"
    CATEGORY = CATEGORY
    DESCRIPTION = ("一个节点出正负两路条件。也可以不用它："
                   "「远程 CLIP 加载器」的输出直接接自带的 CLIPTextEncode 同样能跑。")

    @classmethod
    def VALIDATE_INPUTS(cls, 模型=None, 模型类型=None, input_types=None):
        # v1.7.43：输入端「clip」连着东西时，本节点的 模型/模型类型/地址 全都不参与判断
        # —— 模型名由上游「远程 CLIP 加载器」给，本节点那栏即使是（连接失败，检查服务端）也不该报错。
        if "clip" in (input_types or {}):
            return True
        if 模型 is None:
            return True
        return _validate_model(模型)

    def encode(self, 正向提示词, 负向提示词, 服务端显卡, 计算设备, 服务端地址, 模型,
               模型类型="自动（按文件名判断）", **kw):
        clip = kw.get("clip")
        proxy = None
        handle = None
        token = _cfg_token()
        # v1.7.34：超时限制彻底取消（None = 一直等服务端算完）
        timeout = None
        # 条件张量放哪不再让用户选：一律跟原生节点一致（auto = 本机模型设备）
        device = "auto"

        def to_dev(conds):
            return [[_to_device(c, device), {k: _to_device(v, device) for k, v in d.items()}]
                    for c, d in conds]

        if clip is not None:
            if isinstance(clip, RemoteGGUFClip):
                # v1.7.62：上游是「远程 GGUF 文本编码器」→ 直接用它的编码（服务端 sd.load_clip 读 GGUF）
                print(f"[远程CLIP文本编码] 用上游「远程 GGUF 文本编码器」编码：{clip.gguf}", flush=True)
                return (to_dev(clip.encode_from_tokens_scheduled(clip.tokenize(正向提示词 or ""))),
                        to_dev(clip.encode_from_tokens_scheduled(clip.tokenize(负向提示词 or ""))))
            # 输入端「clip」连了节点：本节点自己的模型字段（地址/模型/模型类型）全部让位，
            # 一律用连过来的这个 clip 编码。
            proxy = clip if isinstance(clip, RemoteCLIP) else None
            handle = _handle_of(clip)
            if proxy is None and handle is None:
                # 本机原生 CLIP（如自带的 CLIPLoader / DualCLIPLoader）：本机直接编码
                print("[远程CLIP编码] 用输入端口连过来的本地 clip 编码（忽略本节点的模型设置）")
                return (to_dev(clip.encode_from_tokens_scheduled(clip.tokenize(正向提示词 or ""))),
                        to_dev(clip.encode_from_tokens_scheduled(clip.tokenize(负向提示词 or ""))))
            if proxy is not None:
                url, tok, name = proxy.server_url, proxy.token, proxy.model
                ctype = proxy.clip_type
            else:
                url, tok = handle.get("url") or 服务端地址, handle.get("token") or token
                name = handle.get("model") or _pick_model(模型, "clip")
                ctype = handle.get("clip_type") or _clip_type_value(模型类型)
        else:
            url, tok = 服务端地址, token
            name = _pick_model(模型, "clip")
            ctype = _clip_type_value(模型类型)

        _check_url(url)
        if proxy is not None or handle is not None:
            print(f"[远程CLIP文本编码] 用输入端口「clip」的模型：{name} @ {url}"
                  f"（type={ctype or 'auto'}，本节点模型设置已忽略）", flush=True)
        clip_proxy = RemoteCLIP(url, tok, name, ctype, timeout=None)

        before_u, after_u = _unload_plan(kw.get("卸载模型"))
        if before_u:
            _unload_remote(url, tok, "clip", name, "文本编码(运行前)", _unload_mode(kw.get("卸载模型")))

        def one(text: str):
            return clip_proxy.encode_from_tokens_scheduled(clip_proxy.tokenize(text or ""))

        try:
            pos = to_dev(one(正向提示词))
            neg = to_dev(one(负向提示词))
        finally:
            if after_u:
                _unload_remote(url, tok, "clip", name, "文本编码(运行后)", _unload_mode(kw.get("卸载模型")))
        return (pos, neg)


# --------------------------------------------------------------------------- #
# 3b. 远程 CLIP 图文编码（文本 + 参考图，图片接口自动增槽）
# --------------------------------------------------------------------------- #
class RemoteCLIPImageEncode:
    """和「远程 CLIP 文本编码」一样的正负两路输出，但多了参考图输入。

    - 图片接口 `图片1…图片10`：前端接一张就自动多出一个空槽（见 web/refresh.js）
    - 接了 `vae`：服务端按 Qwen-Image-Edit 的做法，图缩到「参考图最大像素」后一边进文本编码器
      vision 槽、一边编成 reference_latents 挂到条件上（keep_vision=False）
    - 没接 `vae`：只把图作为 vision 输入（keep_vision=True）
    """

    @classmethod
    def INPUT_TYPES(cls):
        cfg = default_server()
        models, model_opt = _combo("clip", cfg["url"], cfg["token"])
        required = {
            "正向提示词": ("STRING", {"multiline": True, "dynamicPrompts": True, "default": ""}),
            "负向提示词": ("STRING", {"multiline": True, "dynamicPrompts": True, "default": ""}),
            "服务端显卡": ("STRING", {"default": GPU_HINT}),
            "计算设备": ("STRING", {"default": DEV_HINT,
                                 "tooltip": "远端实际在用什么算（显卡 / CPU），点刷新自动识别，只读"}),
            "服务端地址": ("STRING", {"default": cfg["url"]}),
            "模型": (models, {"default": model_opt["default"]}),
            "模型类型": (CLIP_TYPES, {"default": "自动（按文件名判断）"}),
            "参考图最大像素": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 32.0, "step": 0.05,
                                    "tooltip": "单位：百万像素（MP）。参考图等比缩到这个上限"
                                               "（只缩不放，宽高对齐 32），视觉 token 与 reference_latents 共用这一档，"
                                               "默认 1.00 ≈ 1000×1000；0 = 保持原尺寸。"
                                               "服务端视觉编码慢（AMD 那台会回落 CPU）时调到 0.4"}),
            "卸载模型": (UNLOAD_MODES, {"default": UNLOAD_RAM,
                                    "tooltip": "自动=交给服务端；保持加载=跑完不卸；"
                                               "运行后/运行前后自动卸载=编码完就把 CLIP 踢出服务端显存"}),
        }
        optional = {
            "clip": ("CLIP", {"tooltip": "可选：接「远程 CLIP 加载器」的 clip"}),
            "vae": ("VAE", {"tooltip": "接哪个 VAE 都行（远程 VAE 加载器 / 自带的 VAE 加载器）。"
                                    "接了会把参考图编成 reference_latents 挂到条件上（改图而不是重画）；"
                                    "采样 latent 用你自己的 VAE 编码节点编同一张图、denoise 0.55~0.8"}),
        }
        for i in range(1, IMAGE_SLOTS + 1):
            optional[f"图片{i}"] = ("IMAGE", {"tooltip": "参考图；接一张会自动多出一个空接口。"
                                                            "要“按参考图改”，还需 vae + KSampler latent 用同一张图的 VAE 编码"})
        return {"required": required, "optional": optional}

    RETURN_TYPES = ("CONDITIONING", "CONDITIONING")
    RETURN_NAMES = ("正向条件", "负向条件")
    FUNCTION = "encode"
    CATEGORY = CATEGORY
    DESCRIPTION = ("远程 Qwen-VL 图文混编：文字 + 参考图（可选 VAE latent）一次出正负两路条件。"
                   "图片接一张自动多一个接口，最多 10 张。"
                   "采样 latent 请用你自己的 VAE 编码节点编这张参考图（尺寸≤参考图最大像素那一档）。")

    @classmethod
    def VALIDATE_INPUTS(cls, 模型=None, 模型类型=None, input_types=None):
        # v1.7.43：接上「clip」时，本节点自己的模型栏一律不参与校验；
        # 地址/模型名都取输入端口那一份，所以占位符也不会再弹「无效输入」。
        # （只接 vae 时仍然需要本节点的「模型」栏，照旧校验）
        if "clip" in (input_types or {}):
            return True
        if 模型 is None:
            return True
        return _validate_model(模型)

    def encode(self, 正向提示词, 负向提示词, 服务端显卡, 计算设备, 服务端地址, 模型,
               模型类型="自动（按文件名判断）", 参考图最大像素=1.0, **kw):
        clip = kw.get("clip")
        vae = kw.get("vae")
        token = _cfg_token()
        timeout = None  # v1.7.34：无超时限制
        device = "auto"
        # 「参考图最大像素」= 百万像素（MP），换算成服务端要的总像素数；
        # 老工作流里那栏叫「参考图像素」，值是 1048576 这种像素数 —— 两种都认：
        #   · 值 >= 100000 视为「像素数」（老工作流），直接用
        #   · 否则视为 MP（新控件）
        try:
            mp = float(参考图最大像素 or 0)
        except Exception:
            mp = 1.0
        if mp >= 100000:  # 老工作流的像素数被塞进了新控件，按像素理解
            px = int(mp)
            mp = px / 1000000.0
        else:
            px = int(mp * 1000000) if mp > 0 else 0
        legacy = kw.get("参考图像素")
        if legacy not in (None, "") and float(legacy or 0) > 0:
            px = int(float(legacy))
            mp = px / 1000000.0
        imgs = _collect_images(kw)

        def to_dev(conds):
            return [[_to_device(c, device), {k: _to_device(v, device) for k, v in d.items()}]
                    for c, d in conds]

        proxy = clip if isinstance(clip, RemoteCLIP) else None
        vproxy = vae if isinstance(vae, RemoteVAE) else None
        handle = _handle_of(clip)
        vhandle = _handle_of(vae) if not vproxy else None
        # v1.7.47：接的是「本机原生 VAE」（VAE 加载器，不是远程 VAE 加载器）时，
        # 参考图在本机编成 reference_latents —— 不然只有视觉槽、采样器只能重画。
        local_vae = None
        if vae is not None and vproxy is None and vhandle is None:
            local_vae = vae
        ref_lats = (_vae_ref_latents(local_vae, imgs, px)
                    if local_vae is not None else [])

        if isinstance(clip, RemoteGGUFClip):
            # v1.7.62：上游是「远程 GGUF 文本编码器」——只有文本这条路，参考图它装不了视觉塔
            if imgs:
                raise RuntimeError(
                    "接的是「远程 GGUF 文本编码器」，它只有文本、没有视觉塔（GGUF 没法按 sd.load_clip 装 mmproj）。\n"
                    "要带参考图请把 clip 换成「远程 CLIP 加载器」（safetensors 文本编码器），视觉模型走服务端。")
            print(f"[远程CLIP图文编码] 上游是 GGUF 文本编码器 {clip.gguf}：只编文本，无参考图", flush=True)
            return (to_dev(clip.encode_from_tokens_scheduled(clip.tokenize(正向提示词 or ""))),
                    to_dev(clip.encode_from_tokens_scheduled(clip.tokenize(负向提示词 or ""))))

        if clip is not None and proxy is None and handle is None:
            # 输入端「clip」连了本机原生 CLIP：本节点自己的模型字段全部让位，用它来编
            # 有 reference_latents 时按原生做法关掉 vision 槽（keep_vision=False），避免双重插入
            keep = not ref_lats

            def one_local(text: str):
                toks = None
                if imgs:
                    try:
                        toks = clip.tokenize(text or "", images=list(imgs),
                                             keep_vision=keep, prevent_empty_text=True)
                    except TypeError:
                        toks = None
                if toks is None:
                    toks = clip.tokenize(text or "")
                return clip.encode_from_tokens_scheduled(toks)

            print(f"[远程CLIP图文编码] 用输入端口连过来的本地 clip 编码（忽略本节点的模型设置）| "
                  f"图 {len(imgs)} 张 | keep_vision={keep} | 参考图最大像素={mp or 0:g}MP | "
                  f"reference_latents={len(ref_lats) or '无（本机 vae 没接）'}", flush=True)
            return (to_dev(_attach_ref(one_local(正向提示词), ref_lats)),
                    to_dev(_attach_ref(one_local(负向提示词), ref_lats)))

        if proxy is not None:
            url, tok, name = proxy.server_url, proxy.token, proxy.model
            ctype = proxy.clip_type
        elif vproxy is not None and not (服务端地址 or "").strip():
            url, tok = vproxy.server_url, vproxy.token
            name = _pick_model(模型, "clip")
            ctype = _clip_type_value(模型类型)
        elif handle is not None:
            url, tok = handle.get("url") or 服务端地址, handle.get("token") or token
            name = handle.get("model") or _pick_model(模型, "clip")
            ctype = handle.get("clip_type") or _clip_type_value(模型类型)
        else:
            url, tok = 服务端地址, token
            name = _pick_model(模型, "clip")
            ctype = _clip_type_value(模型类型)

        url = (url or "").strip()
        _check_url(url)
        if proxy is not None or handle is not None:
            print(f"[远程CLIP图文编码] 用输入端口「clip」的模型：{name} @ {url}"
                  f"（type={ctype or 'auto'}，本节点的地址/模型设置已忽略）", flush=True)
        clip_proxy = RemoteCLIP(url, tok, name, ctype, timeout=None)

        vae_model = ""
        if vproxy is not None:
            # v1.7.36：VAE 只是「按名字」交给 CLIP 那台服务端去加载，真正干活的永远是 clip 那台机器，
            # 所以这里不需要两边地址字面相等 —— 只做规范化比较，格式差异（http:// / 末尾斜杠 /
            # 大小写 / localhost）一律放过；真的指向另一台机器才提示，并说清怎么改。
            v_url = (vproxy.server_url or "").strip()
            if not v_url or v_url.startswith("<") or v_url.startswith("（"):
                v_url = url  # vae 加载器没填地址：直接沿用 clip 的服务端
            if _norm_url(v_url) != _norm_url(url):
                # 不硬拦：VAE 只是把名字交给 CLIP 那台去加载，只要那台机器上扫得到同名模型就能跑。
                print(f"[远程CLIP图文编码] 注意：「远程 VAE 加载器」填的是 {v_url}，CLIP 用的是 {url}；"
                      f"本次仍按 CLIP 的服务端加载 vae=「{vproxy.model}」。"
                      f"若那边没有这个 vae，把 VAE 加载器的地址改成同一个（或留空）即可。", flush=True)
            vae_model = vproxy.model
        elif vhandle is not None:
            vae_model = vhandle.get("model") or ""
        if ref_lats:
            # 本机 vae 已经编好了 reference_latents：服务端不用再按名字加载 vae
            vae_model = ""

        if not imgs:
            def one(text: str):
                return clip_proxy.encode_from_tokens_scheduled(clip_proxy.tokenize(text or ""))
        else:
            print(f"[远程CLIP图文编码] 图 {len(imgs)} 张 | 参考图最大像素={mp or 0:g}MP（{px or 0} px）"
                  f"| keep_vision={False if ref_lats else 'auto'} | vae={vae_model or '无'}"
                  f"{f' | 本机 vae 已编 reference_latents×{len(ref_lats)}' if ref_lats else ''}", flush=True)
            if not vae_model and not ref_lats:
                print("[远程CLIP图文编码] ⚠ 只接了图、没接 vae：现在只把图塞进视觉槽（keep_vision=True），"
                      "**没有 reference_latents**。\n"
                      "    这种情况下采样器拿到的仍是纯噪声 latent，结果基本等于“照着描述重新画一张”，"
                      "而不是“在原图上改”。\n"
                      "    想按参考图改：① 把 VAE（远程 VAE 加载器 / 本机 VAE 加载器都行）接到本节点的 vae 口；"
                      "② KSampler 的 latent 改用 VAE 编码这张参考图，denoise 0.55~0.85；"
                      "③ 采样尺寸 = 参考图尺寸（用 EmptyLatentImage 时也要同尺寸）。", flush=True)
            else:
                src = "本机 vae" if ref_lats else f"服务端 vae={vae_model}"
                print(f"[远程CLIP图文编码] 已按 Qwen-Image-Edit 的方式编（{src}）："
                      "参考图既进视觉槽、又有 reference_latents 挂到条件上。\n"
                      "    ★ 采样 latent 请用你自己的 VAE 编码节点编**同一张**参考图，"
                      "denoise 0.55~0.8；尺寸跟「参考图最大像素」这一档同量级最稳（约 1000×1000）。\n"
                      "    双侧尺寸差太多会糊/颗粒：参考 latent 是 1MP 档，采样画布也请 1MP 档。", flush=True)

            def one(text: str):
                return clip_proxy.encode_with_images(text or "", imgs, vision_pixels=px,
                                                     vae_model=vae_model,
                                                     keep_vision=False if ref_lats else None)

        before_u, after_u = _unload_plan(kw.get("卸载模型"))
        if before_u:
            _unload_remote(url, tok, "clip", name, "图文编码(运行前)", _unload_mode(kw.get("卸载模型")))
        try:
            return (to_dev(_attach_ref(one(正向提示词), ref_lats)),
                    to_dev(_attach_ref(one(负向提示词), ref_lats)))
        finally:
            if after_u:
                _unload_remote(url, tok, "clip", name, "图文编码(运行后)", _unload_mode(kw.get("卸载模型")))


# --------------------------------------------------------------------------- #
# 3c. 远程提示词优化（Qwen-Image 2.1 风格）：把 GGUF 模型放到远端 LLM 服务上
# --------------------------------------------------------------------------- #
SYS_T2I = (
    "你是 Qwen-Image 2.1 的提示词工程师。根据用户需求写一条可直接用于 Qwen-Image 2.1 文生图的提示词：\n"
    "1) 用自然语言的连贯描述，覆盖主体、动作、外观细节、服装材质、环境、光线、镜头与画风；\n"
    "2) 不要堆砌逗号关键词，不要出现「杰作/8K/最佳画质」这类空话；\n"
    "3) 画面里不要出现文字、水印；\n"
    '只输出严格 JSON：{"prompt": "…", "negative_prompt": "…"}\n'
)
SYS_I2I = (
    "你是 Qwen-Image 2.1 图像编辑模型的提示词工程师。用户给了参考图和修改要求，"
    "你要把要求改写成一条清晰的编辑指令：\n"
    "1) 明确「保留什么」（人物长相、姿势、构图、背景）与「改什么」（服装、材质、颜色、道具、光线）；\n"
    "2) 只描述最终画面，不要解释过程，不要出现「图一/图二」之外的指代混淆；\n"
    "3) 不要写美术风格清单式的空话。\n"
    '只输出严格 JSON：{"prompt": "…", "negative_prompt": "…"}\n'
)
SYS_REV = (
    "你是专业图像描述员。看参考图，反推出一条可直接用于 Qwen-Image 2.1 的提示词，"
    "尽量完整覆盖：主体与数量、外貌与表情、姿势与视角、服装与材质、道具、环境与背景、光线与色调、镜头与画风。\n"
    "不要猜测无法看到的内容，不要写成标签堆叠。\n"
    '只输出严格 JSON：{"prompt": "…", "negative_prompt": "…"}\n'
)

MODE_AUTO = "自动（有图无词=反推 / 有图有词=图生图 / 无图=文生图）"
# ---- 背景策略（v1.7.44） ------------------------------------------------- #
# 「透明背景」开关关掉时，模型仍爱自作主张写“背景处理为完全透明/纯白底”，因为
#  · 系统提示词里哪怕只提一句“若用户要求透明…”它也会照着做；
#  · 用户把上一轮输出（里面带透明背景描述）当成本轮的用户提示词粘回来。
# 所以两件事一起做：① 关掉时给系统提示词一条硬性否定；② 出结果后再扫一遍，把带透明字样的
# 分句整条删掉（只有当用户自己在用户提示词里明确要透明时才不动）。
TRANS_WORDS = (
    "透明", "背景透明", "全透明", "完全透明", "半透明", "透明化", "透明处理",
    "抠图", "去背景", "去除背景", "去背", "纯白背景", "纯白色背景", "纯白底", "白底",
    "alpha", "transparent",
)


def _has_trans(text: str) -> bool:
    s = str(text or "").lower()
    return any(w.lower() in s for w in TRANS_WORDS)


def _scrub_transparency(text: str) -> tuple:
    """把带「透明背景 / 抠图 / 纯白底 …」的分句整条删掉，返回 (新文本, 删掉的条数)。

    先按 。；！? 换行 切句，整句命中就丢；还有漏网的再按逗号切一次。
    """
    if not text:
        return text, 0
    removed = 0

    def cut(src: str, seps: str) -> str:
        nonlocal removed
        parts = re.split("([" + seps + "])", src)
        out = []
        for i in range(0, len(parts), 2):
            content = parts[i]
            sep = parts[i + 1] if i + 1 < len(parts) else ""
            if content.strip() and _has_trans(content):
                removed += 1
                continue
            out.append(content + sep)
        res = "".join(out)
        res = re.sub("[" + seps + "]+(?=[。!?\\n]|$)", "", res)   # 结尾/换行前的空分隔符
        res = re.sub("([" + seps + "]){2,}", r"\1", res)
        return res.strip()

    res = cut(text, "。；！？\\n")
    if _has_trans(res):
        res = cut(res, "，,、")
    return res, removed


# ---- 负向提示词语义（v1.7.45） -------------------------------------------- #
# 负向提示词是「要避免出现的东西」，模型却爱写成「不要改变人物的面部特征、发型…」这种句子。
# 双重否定进了负向里意思正好反了（等于在说“要改变”），所以：
#  ① 系统提示词直接规定：negative_prompt 只写名词短语、不许出现「不要/别/禁止」句式；
#  ② 出结果后再把这类前缀摘掉、拆成词条，兜住模型不听话的情况。
NEG_PREFIXES = tuple(sorted(("不要", "别", "请勿", "切勿", "勿", "禁止", "严禁", "避免",
                             "不应该", "不得", "不能", "不可以", "不该"), key=len, reverse=True))
NEG_SEP = "，,、；;。\n"


def _clean_negative(neg: str) -> tuple:
    """把负向提示词里「不要改变…」这类否定句拆成词条，返回 (新文本, 改动的条数)。"""
    src = str(neg or "").strip()
    if not src:
        return src, 0
    items = [x.strip() for x in re.split("[" + NEG_SEP + "]", src) if x.strip()]
    changed = 0
    out = []
    for it in items:
        hit = False
        for _ in range(3):  # 「不要」「不要再」这类可能叠着写
            t = it.lstrip()
            for p in NEG_PREFIXES:
                if t.startswith(p):
                    t = t[len(p):].lstrip()
                    hit = True
                    break
            else:
                break
            it = t
        it = it.strip(" 。，、；：!?·")
        if hit:
            changed += 1
        if it and it not in out:
            out.append(it)
    if not changed:
        return src, 0
    return "、".join(out), changed

IF_REMOTE = "RemoteCLIPVAE 服务端 (用远端 ComfyUI 里的 GGUF)"
IF_OPENAI = "OpenAI 兼容 (llama.cpp / LM Studio / vLLM)"
IF_OLLAMA = "Ollama 原生 (/api/chat)"
IFACES = [IF_REMOTE, IF_OPENAI, IF_OLLAMA]
NO_MMPROJ = "（不使用 / 纯文本）"
SLOT_EVERY = "自动"
UNLOAD_RAM = "卸载到内存"
UNLOAD_AUTO = "自动"
UNLOAD_KEEP = "保持加载"
UNLOAD_AFTER = "运行后自动卸载"
UNLOAD_BOTH = "运行前后自动卸载"
UNLOAD_MODES = [UNLOAD_RAM, UNLOAD_AUTO, UNLOAD_KEEP, UNLOAD_AFTER, UNLOAD_BOTH]
UNLOAD_DEFAULT = UNLOAD_RAM
# 「种子模式」已并进「种子值」控件的右侧按钮（control_after_generate），这里只留个常量给老工作流做兼容
SEED_MODES = ["随机", "固定"]
DEFAULT_CTX = 4096
DEFAULT_MAX_TOKENS = 2048
DEFAULT_TEMP = 0.7
DEFAULT_TIMEOUT = 600.0


def _opt_json_of(text: str):
    """从模型返回里抠出 JSON（容忍 ```json 包裹、前后废话、思考过程里出现的「格式示例」）。

    关键点：思考过程里常写 `{"prompt": "...", "negative_prompt": "..."}` 这种示例，
    所以不能拿第一个 JSON —— 取【最后一个】真正有内容（prompt 不是 "..."）的对象。
    """
    if not text:
        return None
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    raw = raw.strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and _opt_prompt_of(obj):
            return obj
    except Exception:
        pass
    cands = []
    depth, start = 0, -1
    for i, ch in enumerate(raw):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    o = json.loads(raw[start:i + 1])
                    if isinstance(o, dict):
                        cands.append(o)
                except Exception:
                    pass
                start = -1
    good = [o for o in cands if _opt_prompt_of(o) and len(_opt_prompt_of(o)) > 2]
    return good[-1] if good else (cands[-1] if cands else None)


def _opt_prompt_of(obj: dict) -> str:
    v = obj.get("prompt") or obj.get("positive_prompt") or obj.get("positive") or ""
    v = str(v or "").strip()
    if v in ("...", "…", "…(...)", "（略）", ""):
        return ""
    return v


def _strip_thinking(text: str) -> str:
    """去掉模型在正式回答前的思考段（Thinking Process / <think>…</think> / 推理过程）。"""
    t = (text or "").strip()
    if not t:
        return t
    if "<think" in t:
        # 有闭合标签就取闭合之后；只有开标签（被 max_tokens 截断）就等于没有正文
        t = t.split("</think>", 1)[1].strip() if "</think>" in t else ""
    if not t:
        return ""
    for mark in ("Thinking Process:", "Thinking process:", "推理过程:", "思考过程:"):
        if mark in t:
            t = t.split(mark, 1)[1].strip()
            parts = [p for p in t.split("\n\n") if p.strip()]
            if len(parts) > 1:
                t = parts[-1].strip()
    return t


class RemotePromptOptimizer:
    """把「Qwen image 2.1 提示词优化」的本地 GGUF 加载改成远端 LLM 服务。

    本机只发 HTTP：文本 + 参考图(Base64 PNG) → 远端 OpenAI 兼容接口（llama.cpp server /
    LM Studio / vLLM / Ollama 的 /v1/chat/completions）或 Ollama 原生 /api/chat。
    """

    @classmethod
    def INPUT_TYPES(cls):
        ds = default_server()
        llm_names, llm_err = _model_options("llm", ds["url"], ds["token"])
        if not llm_names:
            llm_names = [FAIL_HINT if llm_err else "（服务端没有 GGUF：先配 GGUF 模型目录）"]
        ds_v, _ = _model_options("llm_vision", ds["url"], ds["token"])
        # 三个模型下拉两边共用：服务端的 GGUF + 本机的 GGUF 都放进候选，
        # 名字各不冲突（本机的是相对路径），填哪个会不会被用只取决于「使用远程模型」开关
        local_names, local_visions = _local_gguf_scan()
        llm_all = list(dict.fromkeys(llm_names + local_names))
        # 占位提示（“连接失败 / 没有 GGUF”）排到最后，默认值优先给真模型
        real = [n for n in llm_all if not n.startswith("（") and not n.startswith("(")]
        hint = [n for n in llm_all if n not in real]
        llm_all = real + hint
        if not llm_all:
            llm_all = ["（没有可用 GGUF：服务端配目录 / 本机放 models/LLM）"]
        llm_default = real[0] if real else llm_all[0]
        vis_all = list(dict.fromkeys(ds_v + local_visions))
        return {
            "required": {
                "用户提示词": ("STRING", {"default": "", "multiline": True, "dynamicPrompts": False}),
                "是否反推": ("BOOLEAN", {"default": False,
                                      "label_on": "开：反推图",
                                      "label_off": "关：系统优化提示词",
                                      "tooltip": "开 = 反推图（必须有参考图）：按系统提示词把图里的内容反推成提示词；"
                                                 "关 = 系统优化提示词：有图走图生图改写、无图走文生图。"
                                                 "两种情况下发给模型的内容都是「系统提示词 + 用户提示词」，"
                                                 "你写的用户提示词会原样一起发出去，不会被替换或丢掉。"}),
                "使用远程模型": ("BOOLEAN", {"default": True,
                                         "label_on": "远程（服务端 GGUF）",
                                         "label_off": "本地（本机 GGUF）",
                                         "tooltip": "开=用服务端那台机器的 GGUF（本机零依赖）；"
                                                   "关=用本机 models/LLM 里的 GGUF（本机要装 llama-cpp-python）"}),
                "服务端地址": ("STRING", {"default": ds["url"],
                                     "tooltip": "远程模式：RemoteCLIPVAE 服务端地址（8199）；模型是服务端 ComfyUI 里 models/LLM 的 GGUF"}),
                "API Key": ("STRING", {"default": "", "tooltip": "一般留空；服务端开了 X-Auth-Token 校验时填那个 token"}),
                "文生图模型": (llm_all, {"default": llm_default,
                                      "tooltip": "【只在没接参考图时用】纯文本分支（文生图）的 GGUF。"
                                                 "接了图会自动改用「图生图模型」，这个就不参与"}),
                "图生图模型": (llm_all, {"default": llm_default,
                                      "tooltip": "【只在接了参考图时用】图生图/反推分支的 GGUF，要配「视觉模型」才能看图。"
                                                 "没接图时自动改用「文生图模型」，不加载视觉塔"}),
                "视觉模型": ([NO_MMPROJ] + vis_all, {"default": NO_MMPROJ,
                                              "tooltip": "mmproj 视觉投影；接图做反推/图生图时要配，纯文本留「不使用」"}),
                "参考图最大像素": ("FLOAT", {"default": 0.4, "min": 0.0, "max": 32.0, "step": 0.05,
                                        "tooltip": "单位：百万像素（MP）。接进来的参考图先等比缩到这个上限再发给模型"
                                                   "（只缩不放，宽高对齐 32）。0 = 不缩。"
                                                   "AMD 那台机视觉编码会回落 CPU，0.4MP≈640×640 只要几秒，"
                                                   "原图 1408×1920(2.7MP) 要 148 秒。觉得反推细节掉了就调到 0.8~1.0"}),
                "输出语言": (["自动", "中文", "English"], {"default": "自动"}),
                "透明背景": ("BOOLEAN", {"default": False,
                                      "tooltip": "关（默认）=背景按原图原样保留，并禁止输出「透明/抠图/纯白底」；"
                                                 "模型硬塞进来的这类分句会被自动删掉（日志里会写删了几处）。"
                                                 "开=要求背景完全透明（保留 alpha）。"
                                                 "如果你自己在「用户提示词」里写了透明/抠图，就按你写的走，不会被删。"}),
                "种子值": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFF,
                                "control_after_generate": True,
                                "tooltip": "种子值 + 右侧那个按钮（固定值/递增值/递减值/随机值）一起构成一个控件："
                                           "选随机值 = 每次排队后自动换一个；选固定值 = 一直用这个数"}),
                "卸载模型": (UNLOAD_MODES, {"default": UNLOAD_RAM,
                                        "tooltip": "远程=服务端 GGUF 的显存策略；本地=跑完是否把本机这份模型放掉。"
                                                   "自动/保持加载=一直留着（下次最快）"}),
            },
            "optional": dict({f"图片{i}": ("IMAGE", {"tooltip": "参考图；接一张自动多一个空接口"})
                              for i in range(1, IMAGE_SLOTS + 1)}),
        }

    RETURN_TYPES = ("STRING", "STRING", "BOOLEAN")
    RETURN_NAMES = ("提示词", "负向提示词", "是否反推")
    FUNCTION = "optimize"
    CATEGORY = CATEGORY
    DESCRIPTION = ("「Qwen image 2.1 提示词优化」。开关打开=用服务端那台机器的 GGUF（本机零依赖）；"
                   "开关关闭=用本机 models/LLM 里的 GGUF（本机需装 llama-cpp-python）。"
                   "「参考图最大像素」（百万像素 MP）在节点这一侧先把参考图等比缩小再发出去，0=不缩。"
                   "「是否反推」开=按系统提示词反推参考图内容（必须有图）；"
                   "关=用内置的系统优化提示词（有图=图生图改写，无图=按需求写文生图提示词）。"
                   "两种情况都会把系统提示词和你写的用户提示词一起发给模型。"
                   "输出：提示词 + 负向提示词 + 是否反推。")

    # ---- 内部 ---------------------------------------------------------- #
    @classmethod
    def VALIDATE_INPUTS(cls, **kw):
        """启动时固化的是「默认地址扫到的 GGUF 列表」，节点里改过地址/点刷新后列表会变，
        队列校验不要拿旧白名单拦人（真正的错误在执行阶段用中文抛出来）。"""
        return True

    @staticmethod
    def _lang_rule(输出语言: str, user_text: str) -> str:
        lang = (输出语言 or "自动").strip()
        if lang == "中文":
            return "\nprompt 与 negative_prompt 必须用简体中文书写。"
        if lang == "English":
            return "\nprompt and negative_prompt must be written in English."
        if user_text and not re.search(r"[\u4e00-\u9fff]", user_text):
            return "\nWrite prompt and negative_prompt in English (following the user's language)."
        return "\nprompt 与 negative_prompt 用简体中文书写。"

    @staticmethod
    def _post_json(url: str, payload: dict, api_key: str, timeout: float) -> dict:
        import urllib.request
        import urllib.error

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:600]
            raise RuntimeError(f"远端 LLM 返回 HTTP {e.code}：{detail}") from None
        except Exception as e:
            raise RuntimeError(f"连不上远端 LLM 服务 {url}：{e}") from None

    # ---- 本地模式：本机 llama-cpp 直接跑 ---- #
    @staticmethod
    def _local_chat(model_path: str, mmproj: str, system: str, text: str, b64s: list,
                    seed: int, max_tokens: int, temperature: float, n_ctx: int):
        import base64
        import tempfile

        llm = _local_llm_get(model_path, mmproj, n_ctx)
        msgs = [{"role": "system", "content": system}]
        tmp_files = []
        if b64s and mmproj:
            parts = [{"type": "text", "text": text}]
            for b in b64s[:4]:
                try:
                    fd, p = tempfile.mkstemp(suffix=".png")
                    with os.fdopen(fd, "wb") as f:
                        f.write(base64.b64decode(b))
                    tmp_files.append(p)
                    parts.append({"type": "image_url", "image_url": {"url": "file:///" + p.replace("\\", "/")}})
                except Exception:
                    break
            msgs.append({"role": "user", "content": parts})
        else:
            msgs.append({"role": "user", "content": text})

        def call(extra: dict):
            return llm.create_chat_completion(messages=msgs, max_tokens=int(max_tokens),
                                               temperature=float(temperature), seed=int(seed), **extra)

        base_extra = {"response_format": {"type": "json_object"},
                      "chat_template_kwargs": {"enable_thinking": False}}
        resp = None
        for drop in (None, "chat_template_kwargs", "response_format"):
            if drop:
                base_extra.pop(drop, None)
            try:
                resp = call(dict(base_extra))
                break
            except TypeError:
                continue
            except ValueError as e:
                if "response_format" in str(e) or "grammar" in str(e):
                    continue
                raise
        for p in tmp_files:
            try:
                os.remove(p)
            except Exception:
                pass
        if resp is None:
            raise RuntimeError("本地 llama-cpp 调用失败（create_chat_completion 全部分支都报错）")
        content = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        usage = resp.get("usage") or {}
        return content, int(usage.get("completion_tokens") or 0)

    def optimize(self, 用户提示词, 是否反推=False, 使用远程模型=True, 服务端地址="", APIKey="", 文生图模型="",
                 图生图模型="", 视觉模型=NO_MMPROJ, 参考图最大像素=0.4,
                 输出语言="自动", 透明背景=False, 种子值=0,
                 卸载模型=UNLOAD_RAM, **kw):
        api_key = str(kw.get("API Key", "") or APIKey or "")
        token = _cfg_token()
        manual = str(kw.get("自定义模型名") or "").strip()  # 老工作流里的旧字段，还有就兜底用
        use_remote = bool(使用远程模型)
        # 老工作流里还有这些控件：有就用，没有就走默认值
        iface = str(kw.get("接口类型") or IF_REMOTE)
        上下文长度 = int(kw.get("上下文长度") or DEFAULT_CTX)
        温度 = float(kw.get("温度", DEFAULT_TEMP) or DEFAULT_TEMP)
        最大输出长度 = int(kw.get("最大输出长度") or DEFAULT_MAX_TOKENS)
        超时时间 = None  # v1.7.34：无超时限制（远端慢慢生成也会一直等）
        images = _collect_images(kw)
        user_text = str(用户提示词 or "").strip()
        has_images = bool(images)
        # v1.7.40：反推由节点上的开关决定，不再按「有图无词」猜。
        # 开 = 按系统提示词 + 用户提示词反推图内容；关 = 走内置的系统优化提示词（图生图 / 文生图）。
        reverse = bool(是否反推)
        if reverse and not has_images:
            raise ValueError(
                "开了「是否反推」但没有接参考图：反推必须有一张图。\n"
                "① 接一张参考图到「图片1」；或 ② 把「是否反推」关掉（关掉=用系统优化提示词写提示词）。")

        if reverse:
            system = SYS_REV
        elif has_images:
            system = SYS_I2I
        else:
            system = SYS_T2I
        system = system + self._lang_rule(输出语言, user_text)
        # v1.7.45：负向提示词必须写「要避免出现的东西」，不是写「不要改变…」的句子
        system += ("\nnegative_prompt 只写「希望画面里避免出现的现象/物件」的名词短语，用顿号分隔，"
                   "例如：面部特征被改变、发型被改变、姿势与构图变化、背景布局变化、多余道具、文字水印。"
                   "严禁以「不要…」「别…」「禁止…」「请勿…」「避免…」开头，也不要写成一句完整的话"
                   "（写「不要改变人物的面部特征」等于在负向里反向要求，是错的）。")
        # Qwen3 系会先吐「Thinking Process」，提示优化只要最终 JSON：直接压掉思考段
        system += ("\n不要输出任何思考过程/推理说明（如 Thinking Process、/no_think 提示），"
                   "第一行就是 {，最后一行就是 }。")
        if 透明背景 and not reverse:
            system += "\n用户要求透明背景：主体之外的环境描述要保证背景是全透明的（alpha=0）。"
        # v1.7.44：背景策略写死给模型 —— 开关关掉时必须明确“不要透明”，否则它爱自作主张
        user_wants_trans = _has_trans(user_text)
        if 透明背景 or user_wants_trans:
            system += ("\n背景处理：背景必须是完全透明的（保留 alpha），主体之外不出现环境、地面、"
                       "墙面与投影；不要写成纯白/纯色背景。")
        else:
            system += ("\n背景处理：背景一律按参考图（没有参考图就按正常实景）原样保留；"
                       "严禁在输出里出现「透明背景 / alpha / 抠图 / 去背景 / 纯白背景 / 白底」"
                       "这类要求，也不要自作主张把背景改成透明或纯色。")
        if reverse:
            # v1.7.42：反推也照常把「用户提示词」原样发过去（不再替换成一句固定指令）。
            # 用户想强调什么（主体/材质/光线…）就写在用户提示词里，系统提示词只负责“这是反推任务”。
            system += ("\n这是一次反推任务：请以参考图为准来描述画面；"
                       "若用户在提示词里提了关注点/限制，必须体现在结果里。")
            if not user_text:
                user_text = "请反推这张图的提示词。"
            if 透明背景:
                user_text = user_text + "\n背景要求透明。"

        # 种子：只有一个控件（INT + 右侧「固定值/递增值/递减值/随机值」）。
        # 选「随机值」时前端每次排队后已经把新种子写进来了，这里直接用「种子值」即可；
        # 老工作流还带「种子模式=随机」的话，仍然按老的随机逻辑走一次。
        种子模式 = str(kw.get("种子模式") or "").strip()
        seed = (random.randint(0, 0xFFFFFFFF) if 种子模式 == "随机"
                else int(种子值 or 0))

        # 参考图：IMAGE → 先按「参考图最大像素」(百万像素) 等比缩小 → 每帧一张 Base64 PNG
        try:
            mp = float(参考图最大像素 or 0)
        except Exception:
            mp = 0.4
        max_pixels = int(mp * 1000000) if mp > 0 else 0
        b64s = []
        for img in images[:IMAGE_SLOTS]:
            for frame in tensor_to_png_frames(img, max_pixels=max_pixels):
                b64s.append(frame)

        unload_after = 卸载模型 in (UNLOAD_AFTER, UNLOAD_BOTH, UNLOAD_RAM)
        unload_before = 卸载模型 == UNLOAD_BOTH

        if not use_remote:
            # ---- 本地模式：本机 models/LLM 的 GGUF，用本机 ComfyUI 的 python 直接跑 ----
            # v1.7.41：严格按「有没有接图」分两套：
            #   接了图 → 图生图模型 + 视觉模型(mmproj)
            #   没接图 → 文生图模型，并且不加载 mmproj（省显存，纯文本用不着视觉塔）
            branch = "图生图" if has_images else "文生图"
            local_pick = str((图生图模型 if has_images else 文生图模型) or "").strip()
            if not local_pick or local_pick.startswith("（") or local_pick.startswith("("):
                local_pick = str(kw.get("本地模型") or "").strip()  # 老工作流里的旧字段
            lpath = _local_gguf_path(local_pick)
            if not lpath:
                raise ValueError(
                    f"本地模式找不到这个 GGUF：{local_pick or '(空)'}\n"
                    f"当前分支是「{branch}」（{'接了参考图' if has_images else '没接参考图'}），"
                    f"应在「{branch}模型」里选一个本机 GGUF。\n"
                    "先把 GGUF 放进本机 <ComfyUI>/models/LLM（也认 models/llm、models/gguf 等）；"
                    "或者把开关「使用远程模型」打开走服务端。"
                )
            mm_local = ""
            if has_images:
                mm_src = str(视觉模型 or "").strip()
                if not mm_src or mm_src.startswith("（") or mm_src.startswith("("):
                    mm_src = str(kw.get("本地视觉模型") or "").strip()  # 老工作流里的旧字段
                if mm_src and not mm_src.startswith("（") and not mm_src.startswith("("):
                    mm_local = _local_gguf_path(mm_src)
                if not mm_local:
                    raise ValueError("本地模式接了参考图，但没选「视觉模型」（mmproj）：本地模型看不了图。\n"
                                     "请把「视觉模型」选成本机对应的 mmproj，或把图片断开走纯文生图。")
            if unload_before:
                _local_llm_free()
            try:
                text, tokens = self._local_chat(lpath, mm_local, system, user_text, b64s, seed,
                                                最大输出长度, 温度, 上下文长度)
            finally:
                if unload_after:
                    _local_llm_free()
            print(f"[提示词优化-本地] 分支={branch} | model={os.path.basename(lpath)} | "
                  f"mmproj={os.path.basename(mm_local) or '无（纯文本）'} | 图 {len(b64s)} 张 | "
                  f"{'反推' if reverse else branch} | seed={seed} | {tokens} tok", flush=True)
            data = {"text": text}
        else:
            url = (服务端地址 or "").strip().rstrip("/")
            if not url:
                raise ValueError("请填写服务端地址（或把开关「使用远程模型」关掉，改用本机 GGUF）")
            timeout = None
            # v1.7.41：同样按「有没有接图」分两套（有图=图生图模型+视觉模型，无图=文生图模型，不传 mmproj）
            branch = "图生图" if has_images else "文生图"
            pick = 图生图模型 if has_images else 文生图模型
            if manual:
                model_name = manual
            else:
                model_name = str(pick or kw.get("模型名") or "").strip()
            if not model_name or model_name.startswith("（") or model_name.startswith("("):
                raise ValueError(f"「{branch}模型」还是占位符：先在服务端面板配好「模型根目录」、启动服务，"
                                 f"再点一下节点上的「🔄 刷新模型列表」，从「{branch}模型」里选一个 GGUF。")
            mmproj = ""
            if has_images:
                mm_raw = str(视觉模型 or "").strip()
                mmproj = "" if (not mm_raw or mm_raw.startswith("（") or mm_raw.startswith("(")) else mm_raw
                if not mmproj:
                    raise ValueError("接了参考图但没有「视觉模型」（mmproj）：服务端只能用纯文本，看不了图。\n"
                                     "请把「视觉模型」选成对应的 mmproj（例如 mmproj-*.gguf），或把图片断开走纯文生图。")

            if unload_before:
                try:
                    _llm_unload_remote(url, token, "提示词优化(运行前)", _unload_mode(卸载模型))
                except Exception:
                    pass

            if iface.startswith("RemoteCLIPVAE"):
                # 走服务端 /rpc → 服务端用它自己 ComfyUI 的 python 加载 <ComfyUI>/models/LLM 里的 GGUF
                try:
                    data = remote_api.rpc(url, token, "llm_chat", {
                        "model": model_name, "mmproj": mmproj,
                        "system": system, "text": user_text, "images": b64s,
                        "max_tokens": int(最大输出长度), "temperature": float(温度),
                        "seed": int(seed), "n_ctx": int(上下文长度 or DEFAULT_CTX),
                        "timeout": None, "auto_load": True,
                    }, timeout=None)
                except RuntimeError as e:
                    raise RuntimeError(f"{e}\n（远端 GGUF 路径：服务端 config.json 的 llm_model_dir，"
                                       f"留空就是 <ComfyUI>/models/LLM；服务端面板「提示词优化 LLM」里能看到模型清单与实时信息）") from None
                finally:
                    if unload_after:
                        try:
                            _llm_unload_remote(url, token, "提示词优化(运行后)", _unload_mode(卸载模型))
                        except Exception:
                            pass
                text = str((data or {}).get("text") or "")
                print(f"[远程提示词优化] RemoteCLIPVAE | 分支={branch} | model={model_name} | "
                      f"mmproj={mmproj or '无（纯文本）'} | 图 {len(b64s)} 张"
                      f"（上限 {mp or 0:g}MP） | "
                      f"{'反推' if reverse else ('图生图' if has_images else '文生图')} | seed={seed} | "
                      f"{data.get('tokens', 0)} tok / {data.get('tps', 0)} tok/s", flush=True)
            elif iface.startswith("Ollama"):
                content = user_text
                msg = {"role": "user", "content": content}
                if b64s:
                    msg["images"] = b64s
                payload = {
                    "model": model_name,
                    "messages": [{"role": "system", "content": system}, msg],
                    "stream": False,
                    "options": {"temperature": float(温度), "seed": int(seed),
                                "num_predict": int(最大输出长度)},
                }
                data = self._post_json(url + "/api/chat", payload, api_key, timeout)
                text = ((data or {}).get("message") or {}).get("content") or ""
                print(f"[远程提示词优化] Ollama | model={model_name} | 图 {len(b64s)} 张 | seed={seed}", flush=True)
            else:
                parts = [{"type": "text", "text": user_text}]
                for b in b64s:
                    parts.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}})
                payload = {
                    "model": model_name,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": parts}],
                    "temperature": float(温度),
                    "max_tokens": int(最大输出长度),
                    "seed": int(seed),
                    "stream": False,
                }
                data = self._post_json(url + "/v1/chat/completions", payload, api_key, timeout)
                try:
                    text = (((data or {}).get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                except Exception:
                    text = ""
                print(f"[远程提示词优化] OpenAI | model={model_name or '(默认)'} | 图 {len(b64s)} 张 | seed={seed}", flush=True)

        if not text.strip():
            raise RuntimeError(f"远端 LLM 没有返回内容：{json.dumps(data, ensure_ascii=False)[:400]}")

        # 思考段一律先剃掉（Qwen3 系会先吐 Thinking Process / <think>）
        text = _strip_thinking(text) if ("Thinking" in text or "思考" in text or "<think" in text) else text
        obj = _opt_json_of(text)
        if isinstance(obj, dict):
            prompt = str(obj.get("prompt") or obj.get("positive_prompt") or obj.get("positive") or "").strip()
            neg = str(obj.get("negative_prompt") or obj.get("negative") or "").strip()
            out_json = json.dumps(obj, ensure_ascii=False, indent=2)
        else:
            clean = text.strip()
            obj2 = _opt_json_of(clean)
            if isinstance(obj2, dict):
                prompt = str(obj2.get("prompt") or obj2.get("positive_prompt") or "").strip()
                neg = str(obj2.get("negative_prompt") or "").strip()
                out_json = json.dumps(obj2, ensure_ascii=False, indent=2)
            else:
                prompt, neg, out_json = clean, "", clean

        if not prompt.strip():
            raise RuntimeError(
                "模型把 token 全用在「思考」上了，没留下正文。\n"
                "换一个不带思考、能直接出 JSON 的 GGUF（或调小「种子值」重跑一次）。"
            )

        if 透明背景 and not reverse and prompt and "透明" not in prompt:
            prompt += "\n背景全透明（alpha=0），不出现地面、墙面与投影。"

        # v1.7.44：开关关着（且用户自己也没要求透明）时，把模型硬塞进来的透明背景分句删掉
        if not 透明背景 and not user_wants_trans:
            prompt, n1 = _scrub_transparency(prompt)
            neg, n2 = _scrub_transparency(neg)
            if n1 or n2:
                print(f"[提示词优化] 透明背景=关：已从输出里去掉 {n1 + n2} 处"
                      f"「透明/抠图/白底」描述（模型自作主张加的）", flush=True)
            if not prompt.strip():
                raise RuntimeError(
                    "模型这条提示词几乎全是「透明背景」内容，删干净后就不剩什么了。\n"
                    "多半是你的「用户提示词」里粘了上一轮带透明背景的输出，或者参考图本身是透明底的 PNG。\n"
                    "把用户提示词改成你真正想要的画面，再跑一次。")
        neg = neg.strip()
        # v1.7.45：负向里「不要改变…」这种句式摘掉前缀、拆成词条（双否定会把意思反过来）
        neg, nneg = _clean_negative(neg)
        if nneg:
            print(f"[提示词优化] 负向提示词里有 {nneg} 条是「不要…」句式（在负向里等于反向要求），"
                  f"已改成词条：{neg}", flush=True)
        return (prompt.strip(), neg, bool(reverse))


# --------------------------------------------------------------------------- #
# 4. 远程 VAE 解码（隐空间 → 图像）
# --------------------------------------------------------------------------- #
class RemoteVAEDecode:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "vae": ("VAE", {"tooltip": "接「远程 VAE 加载器」的 vae（类型就是 VAE）"}),
                "latent": ("LATENT", {"tooltip": "接 KSampler 的 LATENT"}),
                "卸载模型": (UNLOAD_MODES, {"default": UNLOAD_RAM,
                                        "tooltip": "自动=交给服务端；保持加载=跑完不卸；"
                                                   "运行后/运行前后自动卸载=解码完把 VAE 踢出服务端显存"}),
            },
            "optional": {
                "分块解码": ("BOOLEAN", {"default": False, "tooltip": "大图在服务端显存吃紧时开启"}),
                "分块尺寸": ("INT", {"default": 512, "min": 128, "max": 4096, "step": 64}),
                "重叠": ("INT", {"default": 64, "min": 0, "max": 256, "step": 8}),
                "输出设备": (["cpu", "gpu", "auto"], {"default": "cpu",
                                                     "tooltip": "解出的图像放哪；cpu 最省本机显存"}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("图像",)
    FUNCTION = "decode"
    CATEGORY = CATEGORY
    DESCRIPTION = ("和自带的 VAEDecode 等价，只是把解码放到服务端。"
                   "也可以直接用自带的 VAEDecode + 远程 VAE 加载器。")

    def decode(self, vae, latent, 卸载模型=UNLOAD_RAM, 分块解码=False, 分块尺寸=512, 重叠=64, 输出设备="cpu",
               **kw):
        # v1.7.34：无超时限制
        timeout_req = None
        if not isinstance(latent, dict) or "samples" not in latent:
            raise ValueError("latent 需要是 LATENT（含 samples 张量）")
        samples = latent["samples"]
        if isinstance(vae, RemoteVAE):
            vae.timeout = None
            before_u, after_u = _unload_plan(卸载模型)
            if before_u:
                _unload_remote(vae.server_url, vae.token, "vae", vae.model, "VAE 解码(运行前)", _unload_mode(卸载模型))
            try:
                image = vae.decode_tiled(samples, tile_x=分块尺寸, overlap=重叠) if 分块解码 else vae.decode(samples)
            finally:
                if after_u:
                    _unload_remote(vae.server_url, vae.token, "vae", vae.model, "VAE 解码(运行后)", _unload_mode(卸载模型))
            return (_to_device(image, 输出设备),)
        handle = _handle_of(vae)
        if handle is None:
            # v1.7.49：不再只认「远程 VAE 加载器」。接了本机原生 VAE（或任何带 decode 的对象），
            # 就在本机解码，等价于自带 VAEDecode —— 不再因为接错节点直接报错。
            dec = getattr(vae, "decode", None)
            if callable(dec):
                print("[远程VAE解码] 输入口接的是本机 VAE：本次在本机解码（等价自带 VAEDecode）", flush=True)
                return (_to_device(dec(samples), 输出设备),)
            raise ValueError(f"vae 输入无效：{type(vae).__name__} 既不是远程 VAE（远程 VAE 加载器），"
                             f"也没有本机 decode()。请接「远程 VAE 加载器」或自带的「VAE 加载器」。")
        before_u, after_u = _unload_plan(卸载模型)
        url, tok, name = handle["url"], handle.get("token", ""), handle["model"]
        if before_u:
            _unload_remote(url, tok, "vae", name, "VAE 解码(运行前)", _unload_mode(卸载模型))
        try:
            data = remote_api.rpc(
                url, tok, "vae_decode",
                {"model": name, "latent": samples.detach().float().cpu(),
                 "tiled": bool(分块解码), "tile_size": int(分块尺寸), "overlap": int(重叠)},
                timeout=timeout_req,
            )
            image = png_frames_to_tensor(data["frames"], channels=int(data.get("channels") or 3))
        finally:
            if after_u:
                _unload_remote(url, tok, "vae", name, "VAE 解码(运行后)", _unload_mode(卸载模型))
        return (_to_device(image, 输出设备),)


# --------------------------------------------------------------------------- #
# 5. 远程 VAE 编码（图像 → 隐空间）
# --------------------------------------------------------------------------- #
class RemoteVAEEncode:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "vae": ("VAE", {"tooltip": "接「远程 VAE 加载器」的 vae（类型就是 VAE）"}),
                "图像": ("IMAGE", {"tooltip": "例如 LoadImage 的输出"}),
                "卸载模型": (UNLOAD_MODES, {"default": UNLOAD_RAM,
                                        "tooltip": "自动=交给服务端；保持加载=跑完不卸；"
                                                   "运行后/运行前后自动卸载=编码完把 VAE 踢出服务端显存"}),
            },
            "optional": {
                "分块编码": ("BOOLEAN", {"default": False}),
                "分块尺寸": ("INT", {"default": 512, "min": 128, "max": 4096, "step": 64}),
                "重叠": ("INT", {"default": 64, "min": 0, "max": 256, "step": 8}),
            },
        }

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("latent",)
    FUNCTION = "encode"
    CATEGORY = CATEGORY
    DESCRIPTION = ("和自带的 VAEEncode 等价，编码在服务端做。"
                   "也可以直接用自带的 VAEEncode + 远程 VAE 加载器。")

    def encode(self, vae, 图像, 卸载模型=UNLOAD_RAM, 分块编码=False, 分块尺寸=512, 重叠=64, **kw):
        timeout_req = None  # v1.7.34：无超时限制
        if not torch.is_tensor(图像):
            raise ValueError("图像需要是 IMAGE 张量")
        if isinstance(vae, RemoteVAE):
            vae.timeout = None
            before_u, after_u = _unload_plan(卸载模型)
            if before_u:
                _unload_remote(vae.server_url, vae.token, "vae", vae.model, "VAE 编码(运行前)", _unload_mode(卸载模型))
            try:
                latent = vae.encode_tiled(图像, tile_x=分块尺寸, overlap=重叠) if 分块编码 else vae.encode(图像)
            finally:
                if after_u:
                    _unload_remote(vae.server_url, vae.token, "vae", vae.model, "VAE 编码(运行后)", _unload_mode(卸载模型))
            return ({"samples": latent},)
        handle = _handle_of(vae)
        if handle is None:
            # v1.7.49：本机原生 VAE 也能用，本机编码，等价自带 VAEEncode。
            enc = getattr(vae, "encode", None)
            if callable(enc):
                print("[远程VAE编码] 输入口接的是本机 VAE：本次在本机编码（等价自带 VAEEncode）", flush=True)
                return ({"samples": enc(图像)},)
            raise ValueError(f"vae 输入无效：{type(vae).__name__} 既不是远程 VAE（远程 VAE 加载器），"
                             f"也没有本机 encode()。请接「远程 VAE 加载器」或自带的「VAE 加载器」。")
        before_u, after_u = _unload_plan(卸载模型)
        url, tok, name = handle["url"], handle.get("token", ""), handle["model"]
        if before_u:
            _unload_remote(url, tok, "vae", name, "VAE 编码(运行前)", _unload_mode(卸载模型))
        try:
            data = remote_api.rpc(
                url, tok, "vae_encode",
                {"model": name, "frames": tensor_to_png_frames(图像),
                 "tiled": bool(分块编码), "tile_size": int(分块尺寸), "overlap": int(重叠)},
                timeout=timeout_req,
            )
            latent = data["samples"]
        finally:
            if after_u:
                _unload_remote(url, tok, "vae", name, "VAE 编码(运行后)", _unload_mode(卸载模型))
        return ({"samples": latent if torch.is_tensor(latent) else torch.as_tensor(latent)},)


# --------------------------------------------------------------------------- #
# 6. VAE 自检（v1.7.51）：一次跑完就知道“糊/噪点”是 VAE 还是采样参数
# --------------------------------------------------------------------------- #
class RemoteVAESelfCheck:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "vae": ("VAE", {"tooltip": "把你要用的那个 VAE（远程或本机）接进来"}),
                "图像": ("IMAGE", {"tooltip": "原图：LoadImage 的输出"}),
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("往返还原图", "自检报告")
    FUNCTION = "check"
    CATEGORY = CATEGORY
    DESCRIPTION = ("一键自检 VAE：把原图编码再解码回来，算出 PSNR/最大误差，判断"
                   "“整张图噪点/糊”到底是 VAE 不配套、精度不够，还是采样参数的问题。")

    def check(self, vae, 图像):
        lines = []
        img = 图像 if torch.is_tensor(图像) else torch.as_tensor(图像)
        lines.append("===== VAE 自检 =====")
        lines.append(f"VAE 对象: {type(vae).__name__}")

        # 远程 VAE：直接把名字和地址写出来，方便核对是不是配套那个文件
        if isinstance(vae, RemoteVAE):
            lines.append(f"远程 VAE: {vae.model} @ {vae.server_url}")
        for attr in ("latent_channels", "downscale_ratio", "upscale_ratio", "output_channels",
                     "vae_dtype", "crop_input"):
            try:
                lines.append(f"{attr}: {getattr(vae, attr)}")
            except Exception:
                pass
        fs = getattr(vae, "first_stage_model", None)
        if fs is not None:
            lines.append(f"first_stage_model: {type(fs).__name__}")

        enc = getattr(vae, "encode", None)
        dec = getattr(vae, "decode", None)
        if not callable(enc) or not callable(dec):
            lines.append("⚠ 这个 VAE 没有本机 encode/decode，无法自检（远程 VAE 请用本机 VAE 对照测）")
            print("\n".join(lines), flush=True)
            return (img, "\n".join(lines))

        t = img[:1].float()
        t_enc = enc(t)
        lat = t_enc if torch.is_tensor(t_enc) else t_enc.get("samples")
        lat_f = lat.detach().float()
        lines.append(f"latent 形状: {tuple(lat_f.shape)} | std={lat_f.std():.3f} "
                     f"min={lat_f.min():.2f} max={lat_f.max():.2f}")
        rt = dec(lat)
        rt = rt if torch.is_tensor(rt) else torch.as_tensor(rt)
        a = t[..., :3].clamp(0, 1)
        b = rt[..., :3].float().clamp(0, 1)
        h = min(a.shape[1], b.shape[1])
        w = min(a.shape[2], b.shape[2])
        a, b = a[:, :h, :w], b[:, :h, :w]
        mse = float(((a - b) ** 2).mean())
        psnr = 99.0 if mse <= 1e-9 else 10.0 * __import__("math").log10(1.0 / mse)
        lines.append(f"往返 PSNR: {psnr:.2f} dB | 最大误差: {float((a - b).abs().max()):.3f}")

        if psnr >= 32:
            verdict = "VAE 正常：往返几乎无损。→ 噪点/糊来自采样参数或主模型（步数太少、cfg 太低、量化过头）"
        elif psnr >= 26:
            verdict = "VAE 一般：有可见损失。继续跑采样会放大成颗粒。→ 换配套 VAE / 别用 fp8 解码 / 关掉分块解码试试"
        else:
            verdict = "VAE 有问题：往返就已经糊成一片。→ 这个 VAE 与该模型不配套或精度不对，跟采样参数无关"
        lines.append("结论: " + verdict)
        print("[VAE自检]\n" + "\n".join(lines), flush=True)
        return (rt, "\n".join(lines))


# --------------------------------------------------------------------------- #
# 3c. 远程 GGUF：加载器（把服务端 GGUF 装进显存，输出句柄）
# --------------------------------------------------------------------------- #
GGUF_HANDLE_TYPE = "REMOTE_GGUF"
GGUF_SYS_DEFAULT = (
    "你是提示词工程师，为 Qwen-Image 写最终可用的提示词。\n"
    "要求：画面描述具体（主体、动作、镜头、光线、材质、构图），不要空话；\n"
    "只输出 JSON，格式：{\"prompt\": \"...\", \"negative_prompt\": \"...\"}，不要任何解释或前后废话。"
)


class RemoteGGUFLoader:
    """把服务端的 GGUF（llama.cpp 那套）显式装进显存，输出一个句柄给「远程 GGUF 生成」用。

    v1.7.61：以前只有「提示词优化」一个节点，每次跑都要自己 auto_load 一遍，既看不到加载状态，
    也没法给多个下游节点共享同一份已加载的模型。这个节点就是补上的那一半。
    """

    @classmethod
    def INPUT_TYPES(cls):
        cfg = default_server()
        models, model_opt = _combo("llm", cfg["url"], cfg["token"])
        vis = [NO_MMPROJ] + (_model_options("llm_vision", cfg["url"], cfg["token"])[0] or [])
        return {
            "required": {
                "服务端显卡": ("STRING", {"default": GPU_HINT,
                                        "tooltip": "服务端显卡型号，点「🔄 刷新模型列表」自动填，只读"}),
                "计算设备": ("STRING", {"default": DEV_HINT,
                                     "tooltip": "远端实际在用什么算（显卡 / CPU），点刷新自动识别，只读"}),
                "服务端地址": ("STRING", {"default": cfg["url"],
                                        "tooltip": "跑 GGUF 那台机器（RemoteCLIPVAE 服务端）的地址，含端口"}),
                "GGUF 模型": (models, {"default": model_opt["default"],
                                     "tooltip": "服务端已配置的 LLM/GGUF 条目（llm 模型根目录下递归找到的）"}),
                "视觉模型": (vis, {"default": NO_MMPROJ,
                                 "tooltip": "mmproj 视觉投影：要给「生成」节点接参考图才需要选；纯文本留「不使用」"}),
                "上下文长度": ("INT", {"default": DEFAULT_CTX, "min": 512, "max": 131072, "step": 512}),
                "上卡层数": ("INT", {"default": -1, "min": -1, "max": 200,
                                  "tooltip": "-1 = 全部层上卡（推荐）；显存不够时填小一点，剩下的交给 CPU"}),
                "卸载模型": (UNLOAD_MODES, {"default": UNLOAD_RAM,
                                        "tooltip": "自动/保持加载=装好就留着（下游多次调用最快）；"
                                                   "运行后/运行前后=用完就踢出服务端显存"}),
            },
        }

    RETURN_TYPES = (GGUF_HANDLE_TYPE, "STRING")
    RETURN_NAMES = ("gguf", "状态")
    FUNCTION = "load"
    CATEGORY = CATEGORY
    DESCRIPTION = ("加载服务端那台机器的 GGUF（含可选 mmproj 视觉塔），输出句柄。"
                   "「远程 GGUF 生成」接上这个句柄就不用每次重新加载。")

    @classmethod
    def VALIDATE_INPUTS(cls, **kw):
        return True

    def load(self, 服务端地址, **kw):
        模型 = str(kw.get("GGUF 模型") or "")
        视觉模型 = str(kw.get("视觉模型") or NO_MMPROJ)
        上下文长度 = int(kw.get("上下文长度") or DEFAULT_CTX)
        上卡层数 = int(kw.get("上卡层数") if kw.get("上卡层数") is not None else -1)
        卸载模型 = str(kw.get("卸载模型") or UNLOAD_DEFAULT)
        url = (服务端地址 or "").strip()
        _check_url(url)
        model = _require_model(模型, "GGUF")
        mm_raw = str(视觉模型 or "").strip()
        mmproj = "" if (not mm_raw or mm_raw.startswith("（") or mm_raw.startswith("(")
                        or mm_raw == NO_MMPROJ) else mm_raw
        token = _cfg_token()
        before, after = _unload_plan(卸载模型, loader=True)
        if before:
            try:
                _llm_unload_remote(url, token, "GGUF(运行前)", _unload_mode(卸载模型))
            except Exception:
                pass
        try:
            data = remote_api.rpc(url, token, "llm_load", {
                "model": model, "mmproj": mmproj,
                "n_ctx": int(上下文长度 or DEFAULT_CTX),
                "n_gpu_layers": int(上卡层数),
            }, timeout=None) or {}
        except Exception as e:
            raise RuntimeError(
                f"{e}\n（远端 GGUF 路径：服务端 config.json 的 llm_model_dir，留空就是 <ComfyUI>/models/LLM；"
                f"服务端面板「提示词优化 LLM」里能看到模型清单）") from None
        st = str(data.get("state") or "已加载")
        dev = data.get("device_name") or data.get("backend") or ""
        status = (f"{model}" + (f" + {mmproj}" if mmproj else "")
                  + f" | {st}" + (f" | {dev}" if dev else "")
                  + f" | {data.get('handler') or '纯文本'}")
        print(f"[远程GGUF] 已加载：{status}", flush=True)
        handle = {"kind": "remote_gguf", "url": url, "token": token, "model": model,
                  "mmproj": mmproj, "n_ctx": int(上下文长度 or DEFAULT_CTX),
                  "n_gpu_layers": int(上卡层数)}
        if after:
            try:
                _llm_unload_remote(url, token, "GGUF(运行前)", _unload_mode(卸载模型))
            except Exception:
                pass
        return (handle, status)


def _gguf_of(gguf) -> dict:
    if isinstance(gguf, dict):
        return gguf
    raise ValueError("「gguf」输入没接：请把「远程 GGUF 加载器」的输出连到本节点的 gguf 接口"
                     "（不想显式加载，也可以留空，本节点会自己 auto_load）")


def _encode_with_clip(clip, text: str):
    """用一个 CLIP（远程 RemoteCLIP / 老字典句柄 / 本机原生）把文本编成 CONDITIONING。
    远程的走服务端、本机的就地算 —— 和「远程 CLIP 文本编码」节点同一套规矩。"""
    device = "auto"

    def to_dev(conds):
        return [[_to_device(c, device), {k: _to_device(v, device) for k, v in d.items()}]
                for c, d in conds]

    if clip is None:
        return None
    proxy = clip if isinstance(clip, RemoteCLIP) else None
    handle = _handle_of(clip)
    if proxy is not None:
        return to_dev(proxy.encode_from_tokens_scheduled(proxy.tokenize(text or "")))
    if handle is not None and (handle.get("url") or ""):
        cp = RemoteCLIP(handle.get("url"), handle.get("token") or _cfg_token(),
                        handle.get("model") or "", handle.get("clip_type") or "")
        return to_dev(cp.encode_from_tokens_scheduled(cp.tokenize(text or "")))
    enc = getattr(clip, "encode_from_tokens_scheduled", None)
    tok = getattr(clip, "tokenize", None)
    if callable(enc) and callable(tok):
        # 本机原生 CLIP（CLIPLoader / DualCLIPLoader）
        return to_dev(enc(tok(text or "")))
    raise RuntimeError("接进来的 clip 既不是远程 CLIP，也不是原生 CLIP，没法编码（换「远程 CLIP 加载器」）")


# --------------------------------------------------------------------------- #
# 3d. 远程 GGUF 生成：接 gguf（+ 可选 clip）→ 出正负条件 / 正负提示词
# --------------------------------------------------------------------------- #
GGUF_IMAGE_SLOTS = 4


class RemoteGGUFGenerate:
    """用服务端 GGUF 算提示词；接了 clip 就顺手编码成 CONDITIONING，直连 UNet 那条采样链路。

    两种连线：
      A) 远程 GGUF 加载器 →(gguf)→ 本节点 →(正向条件/负向条件)→ KSampler（model 来自 UNet 加载器）
      B) 远程 GGUF 加载器 → 本节点 →(提示词/负向提示词)→ 远程 CLIP 文本编码 → 条件
    不接 gguf 也行：本节点按自己的「服务端地址 + 模型」自己 auto_load 一次。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "用户提示词": ("STRING", {"default": "", "multiline": True, "dynamicPrompts": False}),
                "是否反推": ("BOOLEAN", {"default": False,
                                      "label_on": "开：反推图",
                                      "label_off": "关：按需求写提示词",
                                      "tooltip": "开 = 按系统提示词把参考图内容反推成提示词（必须有图）；"
                                                 "关 = 按用户提示词写/优化提示词，有图时会把图一起发给模型"}),
                "最大输出长度": ("INT", {"default": 1024, "min": 64, "max": 8192, "step": 64}),
                "输出提示词语言": (["跟随用户输入", "英文", "中文"],
                                {"default": "跟随用户输入",
                                 "tooltip": "约束模型输出的正/负向提示词语言。"
                                            "「跟随用户输入」= 用户提示词是中文就出中文，是英文（或没写）就出英文"}),
                "温度": ("FLOAT", {"default": DEFAULT_TEMP, "min": 0.0, "max": 2.0, "step": 0.05}),
                "种子值": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFF,
                                "control_after_generate": True}),
                "参考图最大像素": ("FLOAT", {"default": 0.4, "min": 0.0, "max": 32.0, "step": 0.05,
                                        "tooltip": "单位百万像素(MP)，只缩不放，宽高对齐 32；0=不缩"}),
                "卸载模型": (UNLOAD_MODES, {"default": UNLOAD_RAM}),
            },
            "optional": {
                "gguf": (GGUF_HANDLE_TYPE, {"tooltip": "接「远程 GGUF 加载器」的输出；不接就自己 auto_load"}),
                "clip": ("CLIP", {"tooltip": "可选：接「远程 CLIP 加载器」（或本机 CLIP）。"
                                     "接上就直接输出 CONDITIONING，能连 UNet 采样；"
                                     "不接只输出提示词文本，交给「远程 CLIP 文本编码」"}),
                "系统提示词": ("STRING", {"default": "", "multiline": True,
                                     "tooltip": "留空 = 内置的提示词工程师说明（只输出 JSON）"}),
                **{f"图片{i}": ("IMAGE", {"tooltip": "参考图；接一张自动多一个空接口"})
                   for i in range(1, GGUF_IMAGE_SLOTS + 1)},
            },
        }

    RETURN_TYPES = ("CONDITIONING", "CONDITIONING", "STRING", "STRING")
    RETURN_NAMES = ("正向条件", "负向条件", "提示词", "负向提示词")
    FUNCTION = "generate"
    CATEGORY = CATEGORY
    DESCRIPTION = ("用服务端 GGUF 生成 / 优化提示词。接上 clip 时同时输出 CONDITIONING"
                   "（正向条件→KSampler 正向、负向条件→KSampler 负向，与 UNet 加载器一起构成采样链路）；"
                   "不接 clip 时只出文本，接「远程 CLIP 文本编码」再编码。")

    @classmethod
    def VALIDATE_INPUTS(cls, **kw):
        return True

    def generate(self, 用户提示词="", **kw):
        是否反推 = bool(kw.get("是否反推") or False)
        最大输出长度 = int(kw.get("最大输出长度") or 1024)
        温度 = float(kw.get("温度") if kw.get("温度") is not None else DEFAULT_TEMP)
        种子值 = int(kw.get("种子值") or 0)
        参考图最大像素 = float(kw.get("参考图最大像素") or 0.4)
        卸载模型 = str(kw.get("卸载模型") or UNLOAD_DEFAULT)
        gguf = kw.get("gguf")
        clip = kw.get("clip")
        imgs = [kw.get(f"图片{i}") for i in range(1, GGUF_IMAGE_SLOTS + 1)]
        imgs = [i for i in imgs if i is not None]
        has_images = bool(imgs)

        # ---- 地址 / 模型 / mmproj 全部来自「远程 GGUF 加载器」的句柄 ----
        if isinstance(gguf, dict):
            url = str(gguf.get("url") or "")
            token = str(gguf.get("token") or _cfg_token())
            model = str(gguf.get("model") or "")
            mmproj = str(gguf.get("mmproj") or "")
            n_ctx = int(gguf.get("n_ctx") or DEFAULT_CTX)
            ngl = int(gguf.get("n_gpu_layers") if gguf.get("n_gpu_layers") is not None else -1)
        else:
            raise ValueError("这个节点要接「远程 GGUF 加载器」的 gguf 输出（v1.7.66 起已去掉本机自填的"
                             "服务端地址/GGUF 模型/视觉模型）：把加载器的 gguf 连到本节点的 gguf 输入即可，"
                             "地址、模型、mmproj 全部沿用加载器里选的。")
        _check_url(url)

        # ---- 提示词 ----
        system = str(kw.get("系统提示词") or "").strip() or GGUF_SYS_DEFAULT
        user_text = str(用户提示词 or "").strip()
        语言 = str(kw.get("输出提示词语言") or "跟随用户输入")
        if 语言 not in ("中文", "英文"):  # 「跟随用户输入」
            语言 = "中文" if re.search(r"[\u4e00-\u9fff]", user_text) else "英文"
        if 语言 == "中文":
            system += ("\n输出语言要求：prompt 与 negative_prompt 的内容必须全部用简体中文书写，"
                       "不要输出英文提示词。")
        else:
            system += ("\n输出语言要求：prompt 与 negative_prompt 的内容必须全部用英文书写，"
                       "不要输出中文提示词。")
        if 是否反推:
            if not has_images:
                raise ValueError("「是否反推」开着但没有参考图：接一张「图片1」，或者把开关关掉。")
            system += "\n这是一次反推任务：以参考图为准描述画面，用户补充的要点必须体现在结果里。"
            if not user_text:
                user_text = "请反推这张图的提示词。"
        elif not user_text and not has_images:
            raise ValueError("「用户提示词」是空的：写一句你想画什么，或者接参考图 + 打开「是否反推」。")

        # ---- 参考图：等比缩到「参考图最大像素」→ Base64 PNG ----
        try:
            mp = float(参考图最大像素 or 0)
        except Exception:
            mp = 0.4
        max_pixels = int(mp * 1000000) if mp > 0 else 0
        b64s = []
        for img in imgs:
            for frame in tensor_to_png_frames(img, max_pixels=max_pixels):
                b64s.append(frame)
        if has_images:
            if not mmproj:
                raise ValueError("接了参考图但没有「视觉模型」（mmproj）：服务端只能用纯文本，看不了图。\n"
                                 "请把「视觉模型」选成对应的 mmproj（例如 mmproj-*.gguf），或把图片断开走纯文本。")

        before, after = _unload_plan(卸载模型)
        if before:
            try:
                _llm_unload_remote(url, token, "GGUF(运行前)", _unload_mode(卸载模型))
            except Exception:
                pass
        try:
            data = remote_api.rpc(url, token, "llm_chat", {
                "model": model, "mmproj": mmproj,
                "system": system, "text": user_text, "images": b64s,
                "max_tokens": int(最大输出长度), "temperature": float(温度),
                "seed": int(种子值 or 0), "n_ctx": n_ctx,
                "timeout": None, "auto_load": True,
            }, timeout=None) or {}
        except RuntimeError as e:
            raise RuntimeError(
                f"{e}\n（这个 GGUF 来自「远程 GGUF 加载器」的句柄，走的是服务端 llm_model_dir）"
            ) from None
        finally:
            if after:
                try:
                    _llm_unload_remote(url, token, "提示词优化(运行前)", _unload_mode(卸载模型))
                except Exception:
                    pass

        text = str((data or {}).get("text") or "")
        if not text.strip():
            raise RuntimeError(f"服务端 GGUF 没有返回内容：{json.dumps(data, ensure_ascii=False)[:400]}")
        if ("Thinking" in text or "思考" in text or "<think" in text):
            text = _strip_thinking(text)
        obj = _opt_json_of(text)
        if isinstance(obj, dict):
            prompt = str(obj.get("prompt") or obj.get("positive_prompt") or obj.get("positive") or "").strip()
            neg = str(obj.get("negative_prompt") or obj.get("negative") or "").strip()
        else:
            prompt, neg = text.strip(), ""
        if not prompt:
            raise RuntimeError("模型把 token 都用在思考上了，没留下正文：把「最大输出长度」调大（比如 2048）再试。")
        print(f"[远程GGUF生成] {os.path.basename(model)} | 图 {len(imgs)} 张"
              f"{'（mmproj=' + os.path.basename(mmproj) + '）' if mmproj else ''} | "
              f"{'反推' if 是否反推 else '写词'} | {data.get('tokens', 0)} tok / "
              f"{data.get('tps', 0)} tok/s | 正向 {len(prompt)} 字 / 负向 {len(neg)} 字", flush=True)

        # ---- 可选：直接编码成 CONDITIONING（给 UNet 采样用） ----
        if clip is not None:
            pos = _encode_with_clip(clip, prompt)
            negc = _encode_with_clip(clip, neg or "")
            print("[远程GGUF生成] 已用接进来的 clip 编码：正向条件 / 负向条件 可直接接 KSampler",
                  flush=True)
        else:
            pos = negc = None
            print("[远程GGUF生成] 没接 clip：只输出提示词文本 → 接到「远程 CLIP 文本编码」再出条件",
                  flush=True)
        return (pos, negc, prompt, neg)


# --------------------------------------------------------------------------- #
# 3b-2. 远程 GGUF 当文本编码器（输出 CLIP，可直接接原生 CLIPTextEncode / Qwen 编辑节点）
# --------------------------------------------------------------------------- #
class RemoteGGUFClip:
    """服务端 .gguf（Qwen2.5-VL 这类文本编码器）的 CLIP 代理。

    v1.7.62：用户要的是「加载远程 GGUF 输出一个 clip 模型」—— 这个对象既是 CLIP，
    又不会在本机加载任何权重：tokenize/编码都在服务端做（服务端 sd.load_clip 读 GGUF，
    需要 ComfyUI-GGUF 那套 ops）。
    故意**不继承 RemoteCLIP**：继承的话，插件里那几个编码节点会把它当普通远程 CLIP，
    拿模型名去 CLIP 清单里找，结果找不到。
    """

    is_gguf_clip = True

    def __init__(self, url: str, token: str, gguf: str, clip_type: str = "", last_layer: int = 0):
        self.remote = True
        self.server_url = url
        self.token = token or ""
        self.model = gguf
        self.gguf = gguf
        self.clip_type = clip_type or ""
        self.last_layer = int(last_layer or 0)
        self.device = torch.device("cpu")
        self.layer_idx = None
        self.patcher = None
        self.cond_stage_model = None
        self.use_clip_schedule = False
        self.options = {}
        self._img_cache = {}

    def __repr__(self) -> str:
        return f"<RemoteGGUFClip gguf={self.gguf!r} server={self.server_url!r}>"

    def tokenize(self, text, return_word_ids=False, images=None, keep_vision=None,
                 prevent_empty_text=False, llama_template=None, **kwargs):
        payload = {"text": text if isinstance(text, str) else str(text),
                   "keep_vision": None if keep_vision is None else bool(keep_vision),
                   "prevent_empty_text": bool(prevent_empty_text),
                   "llama_template": llama_template if isinstance(llama_template, str) else ""}
        if images:
            payload["images"] = RemoteCLIP._pack_images(self, images)
        return {"__remote_clip_text__": payload["text"], "__remote_clip__": payload}

    def encode_from_tokens_scheduled(self, tokens, opts=None):
        text = RemoteCLIP._text_of(tokens)
        payload = {"model": self.gguf, "text": text, "clip_type": self.clip_type,
                   "last_layer": self.last_layer}
        if isinstance(tokens, dict) and "__remote_clip__" in tokens:
            p = tokens["__remote_clip__"]
            if p.get("images"):
                raise RuntimeError(
                    "GGUF 文本编码器这条链路不支持参考图：mmproj 视觉塔没法按 sd.load_clip 装进 CLIP。\n"
                    "要带图请改用「远程 CLIP 图文编码」（safetensors 文本编码器 + 可选 VAE）。")
            if p.get("llama_template"):
                payload["llama_template"] = p["llama_template"]
        try:
            data = remote_api.rpc(self.server_url, self.token, "gguf_encode", payload, timeout=None)
        except RuntimeError:
            raise
        except Exception as e:
            raise _connect_error(self.server_url, e) from None
        return _norm_conditioning((data or {}).get("conditioning"))

    def encode_from_tokens(self, tokens, return_pooled=False, return_dict=False):
        conds = self.encode_from_tokens_scheduled(tokens)
        cond, extra = conds[0]
        pooled = extra.get("pooled_output")
        if return_dict:
            d = {"cond": cond}
            d.update(extra)
            return d
        if return_pooled:
            return cond, pooled
        return cond

    def encode(self, text, **kwargs):
        return self.encode_from_tokens_scheduled(self.tokenize(text))

    def clip_layer(self, layer_idx: int):
        self.last_layer = int(layer_idx)
        self.layer_idx = int(layer_idx)
        return self

    def clone(self):
        return RemoteGGUFClip(self.server_url, self.token, self.gguf, self.clip_type, self.last_layer)

    def set_clip_options(self, options: dict):
        self.options = dict(options or {})

    def get_clip_options(self):
        return dict(self.options)


class RemoteGGUFCLIPLoader:
    """加载服务端 GGUF 当文本编码器，输出 clip（接着就能用原生 CLIPTextEncode 编码）。"""

    @classmethod
    def INPUT_TYPES(cls):
        cfg = default_server()
        models, model_opt = _combo("llm", cfg["url"], cfg["token"])
        return {
            "required": {
                "服务端显卡": ("STRING", {"default": GPU_HINT}),
                "计算设备": ("STRING", {"default": DEV_HINT,
                                     "tooltip": "远端实际在用什么算，点刷新自动识别，只读"}),
                "服务端地址": ("STRING", {"default": cfg["url"]}),
                "GGUF 模型": (models, {"default": model_opt["default"],
                                     "tooltip": "服务端 LLM 目录里当「文本编码器」用的 GGUF（如 Qwen-Image 的 PE 模型）"}),
                "模型类型": (CLIP_TYPES, {"default": "qwen_image",
                                        "tooltip": "GGUF 文本编码器的架构。Qwen-Image 系列用 qwen_image；不确定时按服务端日志报的类型改"}),
                "卸载模型": (UNLOAD_MODES, {"default": UNLOAD_RAM}),
            },
        }

    RETURN_TYPES = ("CLIP", "STRING")
    RETURN_NAMES = ("clip", "状态")
    FUNCTION = "load"
    CATEGORY = CATEGORY
    DESCRIPTION = ("把服务端 LLM 目录里的 .gguf 当文本编码器加载，输出 CLIP："
                   "可直接接原生「CLIP 文本编码」「文本编码 Qwen Image 编辑 Plus」等节点，"
                   "编码在服务端跑（服务端 ComfyUI 需装 ComfyUI-GGUF）。只支持文本，参考图请用「远程 CLIP 图文编码」。")

    @classmethod
    def VALIDATE_INPUTS(cls, **kw):
        return True

    def load(self, 服务端地址, **kw):
        模型名 = str(kw.get("GGUF 模型") or "")
        模型类型 = str(kw.get("模型类型") or "qwen_image")
        卸载模型 = str(kw.get("卸载模型") or UNLOAD_DEFAULT)
        url = (服务端地址 or "").strip()
        _check_url(url)
        model = _require_model(模型名, "GGUF")
        token = _cfg_token()
        ctype = _clip_type_value(模型类型) or "qwen_image"
        before, after = _unload_plan(卸载模型, loader=True)
        if before:
            try:
                remote_api.rpc(url, token, "unload", {"kind": "clip", "model": model,
                                                      "mode": _unload_mode(卸载模型)}, timeout=None)
            except Exception:
                pass
        try:
            remote_api.health(url, token, timeout=None)
            # 先让服务端真加载一次：读不了 GGUF（缺 ComfyUI-GGUF）在这里就报出来，不用等编码
            remote_api.rpc(url, token, "gguf_encode", {"model": model, "text": "ping",
                                                      "clip_type": ctype, "last_layer": 0}, timeout=None)
        except Exception as e:
            raise RuntimeError(
                f"{e}\n（服务端 ComfyUI 必须装 ComfyUI-GGUF（city96）才能把 .gguf 当文本编码器加载；"
                f"装好后重启 ComfyUI 与 RemoteCLIPVAE 服务端再试）") from None
        clip = RemoteGGUFClip(url, token, model, ctype)
        status = f"{model} | clip_type={ctype} | 服务端文本编码器（GGUF）"
        print(f"[远程GGUF-CLIP] 句柄就绪：{url} | {status}", flush=True)
        if after:
            try:
                remote_api.rpc(url, token, "unload", {"kind": "clip", "model": model,
                                                      "mode": _unload_mode(卸载模型)}, timeout=None)
            except Exception:
                pass
        return (clip, status)


# ==================================================================== #
# v1.7.64：GGUF 当扩散模型（DiT/UNet）—— 加载在服务端，采样也在服务端
# ==================================================================== #

try:  # 采样器/调度器清单直接取 ComfyUI 自己的，保证名字完全一致
    import comfy.samplers as _cs

    SAMPLER_NAMES = list(_cs.KSampler.SAMPLERS) or ["euler"]
    SCHEDULER_NAMES = list(_cs.KSampler.SCHEDULERS) or ["normal"]
except Exception:  # 单元测试环境
    SAMPLER_NAMES = ["euler", "euler_ancestral", "dpmpp_2m", "dpmpp_2m_sde", "uni_pc", "ddim", "lms", "heun"]
    SCHEDULER_NAMES = ["normal", "karras", "exponential", "sgm_uniform", "simple", "ddim_uniform", "beta"]


def _deep_cpu(obj):
    """把条件/latent 里的张量搬到 CPU，好让服务端反序列化（不然 cuda 张量存不下来）。"""
    try:
        import torch as _torch
    except Exception:
        return obj
    if _torch.is_tensor(obj):
        return obj.detach().cpu()
    if isinstance(obj, dict):
        return {k: _deep_cpu(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(_deep_cpu(v) for v in obj)
    return obj


NODE_CLASS_MAPPINGS = {
    "RemoteCLIPLoader": RemoteCLIPLoader,
    "RemoteVAELoader": RemoteVAELoader,
    "RemoteCLIPTextEncode": RemoteCLIPTextEncode,
    "RemoteCLIPImageEncode": RemoteCLIPImageEncode,
    "RemotePromptOptimizer": RemotePromptOptimizer,
    "RemoteVAEDecode": RemoteVAEDecode,
    "RemoteVAEEncode": RemoteVAEEncode,
    "RemoteVAESelfCheck": RemoteVAESelfCheck,
    "RemoteGGUFLoader": RemoteGGUFLoader,
    "RemoteGGUFGenerate": RemoteGGUFGenerate,
    "RemoteGGUFCLIPLoader": RemoteGGUFCLIPLoader,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "RemoteCLIPLoader": "远程 CLIP 加载器 (Remote CLIP Loader)",
    "RemoteVAELoader": "远程 VAE 加载器 (Remote VAE)",
    "RemoteCLIPTextEncode": "远程 CLIP 文本编码 (Remote CLIP Encode)",
    "RemoteCLIPImageEncode": "远程 CLIP 图文编码 (Remote CLIP Encode + Images)",
    "RemotePromptOptimizer": "提示词优化 (Prompt Optimizer, Remote)",
    "RemoteVAEDecode": "远程 VAE 解码 (Remote VAE Decode)",
    "RemoteVAEEncode": "远程 VAE 编码 (Remote VAE Encode)",
    "RemoteVAESelfCheck": "VAE 自检 (VAE Self Check)",
    "RemoteGGUFLoader": "远程 GGUF 加载器 (Remote GGUF Loader)",
    "RemoteGGUFGenerate": "远程 GGUF 生成 (Remote GGUF Generate → CLIP / UNet)",
    "RemoteGGUFCLIPLoader": "远程 GGUF 文本编码器 (Remote GGUF → CLIP)",
}
