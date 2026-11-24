# -*- coding: utf-8 -*-
"""本地自检：在不启动 ComfyUI 的情况下，把整个包 import 一遍，核对节点表。

用 ComfyUI 的 python 跑（要能 import torch）：
    D:\\Comfy-Desktop\\ComfyUI\\ComfyUI\\.venv\\Scripts\\python.exe selftest_local.py
退出码 0 = 通过。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.abspath(__file__))


def _stub(name: str, **attrs) -> None:
    if name in sys.modules:
        return
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod


# ComfyUI 进程里才有 server / folder_paths，这里给最小替身
class _PromptServer:
    instance = None


_stub("server", PromptServer=_PromptServer)
_stub("folder_paths", get_input_directory=lambda: os.path.join(ROOT, "input"))

PKG = "comfyui_remoteclipvae_selftest"
spec = importlib.util.spec_from_file_location(PKG, os.path.join(ROOT, "__init__.py"),
                                              submodule_search_locations=[ROOT])
mod = importlib.util.module_from_spec(spec)
sys.modules[PKG] = mod
spec.loader.exec_module(mod)

mappings = mod.NODE_CLASS_MAPPINGS
names = mod.NODE_DISPLAY_NAME_MAPPINGS
print(f"节点数 = {len(mappings)}")
for k in sorted(mappings):
    print(f"  {k:<34} {names.get(k, '')}")

assert len(mappings) >= 15, f"节点数偏少：{len(mappings)}"
assert all(k in names for k in mappings), "有节点缺少显示名"
assert mod.WEB_DIRECTORY == "./web", mod.WEB_DIRECTORY
assert os.path.isdir(os.path.join(ROOT, "web")), "缺少 web 目录"
print("自检通过 ✔")
