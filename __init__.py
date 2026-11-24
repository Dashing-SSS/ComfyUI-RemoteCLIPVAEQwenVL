# -*- coding: utf-8 -*-
"""ComfyUI-RemoteCLIPVAE —— 双机分离套件的整包入口（Comfy Registry 发布版）。

一个包，两套节点：
    remote_clip_vae/   远程 CLIP / VAE / 文本编码 / 图文编码 / 提示词优化（走 RemoteCLIPVAE 服务端）
    qwenvl_remote/     远程 QwenVL 加载器 / 图生文 / 提示词增强 / 文本净化 / 视频加载器
    model_server/      服务端源码（RemoteCLIPVAE 服务端，独立 exe 也可单独跑）
    web/               两套节点的前端扩展（刷新按钮、动态图片口、视频列表刷新）

ComfyUI 只认本文件导出的 NODE_CLASS_MAPPINGS / NODE_DISPLAY_NAME_MAPPINGS / WEB_DIRECTORY；
两个子包自己的 __init__ 里已经注册好各自的 HTTP 路由，import 时即生效。
"""

from __future__ import annotations

__version__ = "1.0.0"

from .remote_clip_vae import NODE_CLASS_MAPPINGS as _M_RCV, NODE_DISPLAY_NAME_MAPPINGS as _D_RCV
from .qwenvl_remote import NODE_CLASS_MAPPINGS as _M_QWENVL, NODE_DISPLAY_NAME_MAPPINGS as _D_QWENVL

NODE_CLASS_MAPPINGS: dict = {}
NODE_DISPLAY_NAME_MAPPINGS: dict = {}

for _m, _d in ((_M_RCV, _D_RCV), (_M_QWENVL, _D_QWENVL)):
    for _k, _v in _m.items():
        if _k in NODE_CLASS_MAPPINGS:
            print(f"[remoteclipvae] 节点键冲突，跳过重复项：{_k}", flush=True)
            continue
        NODE_CLASS_MAPPINGS[_k] = _v
        NODE_DISPLAY_NAME_MAPPINGS[_k] = _d.get(_k, _k)

WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY", "__version__"]

print(f"[remoteclipvae] 已加载 {len(NODE_CLASS_MAPPINGS)} 个节点（远程 CLIP/VAE {len(_M_RCV)} 个 · "
      f"QwenVL 远程 {len(_M_QWENVL)} 个）· v{__version__}", flush=True)
