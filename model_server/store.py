# -*- coding: utf-8 -*-
"""store.py —— v1.7.54：exe 版的设置存储（不生成任何文件）。

两种情况：

1) **exe 版**（`sys.frozen` 为真）：config.json / gui_state.json / _python.txt / _comfy.txt / _smoke.txt
   这些内容**全部写进 Windows 注册表** `HKEY_CURRENT_USER\\Software\\RemoteCLIPVAE`，
   exe 旁边不会再出现任何配置文件或记录文件。启动服务时配置也**不落盘**，
   直接用 JSON 字符串通过环境变量 `RCLIPVAE_CONFIG_JSON` 传给服务端子进程。

2) **源码版**（直接跑 python model_server/gui.py）：行为跟以前一样，还是读写同目录下的文件，
   方便调试时用编辑器直接看。

对外 API 与原来的 `load_json / save_json / _read_text / _write_text` 完全同形，调用方不用改逻辑。
"""

from __future__ import annotations

import copy
import json
import os
import sys

APP_KEY = r"Software\RemoteCLIPVAE"
ENV_CFG = "RCLIPVAE_CONFIG_JSON"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


# --------------------------------------------------------------------------- #
# 注册表读写（只在 exe 版用；读失败一律当作「没有」）
# --------------------------------------------------------------------------- #
def _reg_read(name: str):
    try:
        import winreg
    except Exception:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APP_KEY) as k:
            val, _typ = winreg.QueryValueEx(k, name)
            return val
    except OSError:
        return None
    except Exception:
        return None


def _reg_write(name: str, text: str) -> None:
    import winreg
    k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, APP_KEY)
    try:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, text)
    finally:
        try:
            winreg.CloseKey(k)
        except Exception:
            pass


def _reg_delete(name: str) -> None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APP_KEY, 0, winreg.KEY_ALL_ACCESS) as k:
            winreg.DeleteValue(k, name)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #
def load_json(path: str, default: dict) -> dict:
    if is_frozen():
        raw = _reg_read(os.path.basename(path))
        if raw:
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass
        return copy.deepcopy(default)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return copy.deepcopy(default)


def save_json(path: str, obj: dict) -> None:
    if is_frozen():
        _reg_write(os.path.basename(path), json.dumps(obj, ensure_ascii=False))
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# 纯文本（_python.txt / _comfy.txt / _smoke.txt 这类记录）
# --------------------------------------------------------------------------- #
def read_text(path: str) -> str:
    if is_frozen():
        val = _reg_read(os.path.basename(path))
        return str(val or "").strip()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def write_text(path: str, text: str) -> None:
    if is_frozen():
        _reg_write(os.path.basename(path), text or "")
        return
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def delete_text(path: str) -> None:
    if is_frozen():
        _reg_delete(os.path.basename(path))
        return
    try:
        os.remove(path)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 服务端子进程拿配置：exe 版走环境变量，绝不写临时文件
# --------------------------------------------------------------------------- #
def config_env_value(cfg: dict) -> str:
    return json.dumps(cfg, ensure_ascii=False)


def read_config_from_env() -> dict | None:
    raw = os.environ.get(ENV_CFG) or ""
    if not raw.strip():
        return None
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def describe_backend() -> str:
    return ("Windows 注册表 HKCU\\" + APP_KEY) if is_frozen() else "本地文件"
