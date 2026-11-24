# -*- coding: utf-8 -*-
"""ComfyUI-QwenVL-Remote —— QwenVL 全远程版（本机不加载任何模型权重）。

思路：原版 ComfyUI-QwenVL 把 QwenVL 权重读进本机（transformers 或 llama.cpp）；
这一份把「加载模型」换成「连 RemoteCLIPVAE 服务端」，推理全在服务端跑：
    GET  /health                 取模型清单（llm / llm_vision）与显卡型号
    POST /rpc op=llm_chat        发文本 + PNG(base64) 图片，拿回文本
    POST /rpc op=llm_unload      让服务端把 LLM 退出显存
"""

from __future__ import annotations

import json
import os

from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS  # noqa: F401
from .api import register as register_routes

WEB_DIRECTORY = "./web"

# 没有 remote_server.json 就生成一份默认的（界面里也能直接改「服务端地址」）
try:
    _cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "remote_server.json")
    if not os.path.exists(_cfg):
        with open(_cfg, "w", encoding="utf-8") as f:
            json.dump({"url": "http://10.10.10.2:8199", "token": ""}, f, ensure_ascii=False, indent=2)
except Exception:
    pass

register_routes()

print(f"[QwenVL 远程] {len(NODE_CLASS_MAPPINGS)} 个节点已加载："
      f"{' / '.join(NODE_DISPLAY_NAME_MAPPINGS.values())}")

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
