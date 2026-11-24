# -*- coding: utf-8 -*-
"""wire.py —— 和服务端 /rpc 通信用的最小序列化协议。

服务端（RemoteCLIPVAE）用 codec.py：
    dict(str键) -> {"__d__": {...}}
    list        -> {"__l__": [...]}
    tuple       -> {"__p__": [...]}
    torch.Tensor-> {"__t__": ...}
    bytes       -> {"__b__": ...}
    标量/None   -> 原样
这里实现 pack/unpack 的子集：远程 QwenVL 只传字符串、数字、列表，不传张量。
"""

from __future__ import annotations


def pack(o):
    if isinstance(o, dict):
        return {"__d__": {str(k): pack(v) for k, v in o.items()}}
    if isinstance(o, tuple):
        return {"__p__": [pack(x) for x in o]}
    if isinstance(o, list):
        return {"__l__": [pack(x) for x in o]}
    if isinstance(o, (str, int, float, bool)) or o is None:
        return o
    return str(o)


def unpack(o):
    if isinstance(o, dict):
        if "__d__" in o and len(o) == 1:
            return {k: unpack(v) for k, v in (o["__d__"] or {}).items()}
        if "__l__" in o and len(o) == 1:
            return [unpack(x) for x in (o["__l__"] or [])]
        if "__p__" in o and len(o) == 1:
            return tuple(unpack(x) for x in (o["__p__"] or []))
        if "__t__" in o and len(o) == 1:
            return o  # 本插件不需要张量；原样留着，别炸
        return {k: unpack(v) for k, v in o.items()}
    if isinstance(o, list):
        return [unpack(x) for x in o]
    return o
