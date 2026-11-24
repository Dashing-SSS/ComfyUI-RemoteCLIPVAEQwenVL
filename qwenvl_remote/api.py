# -*- coding: utf-8 -*-
"""api.py —— 给前端「🔄 刷新模型列表」用的本机代理路由。

浏览器直接跨域请求服务端会被挡，所以前端只打 ComfyUI 本机这两个接口：

    GET /qwenvl_remote/models?url=...   -> {"ok":true,"llm":[...],"llm_vision":[...],"device":"..."}
    GET /qwenvl_remote/unload?url=...   -> {"ok":true,...}
"""

from __future__ import annotations

from . import remote_api
from . import video as video_mod


def _models(url: str, token: str) -> dict:
    try:
        info = remote_api.health(url, token, 5.0)
    except Exception as e:
        return {"ok": False, "error": str(e), "llm": [], "llm_vision": [], "device": ""}
    models = info.get("models") or {}
    llm = sorted((models.get("llm") or {}).keys())
    vis = sorted((models.get("llm_vision") or {}).keys())
    err = ""
    if not llm:
        err = "服务端 models.llm 是空的：把 .gguf 放到服务端 models/LLM 下，再点服务端界面的扫描"
    return {"ok": True, "llm": llm, "llm_vision": vis,
            "device": str(info.get("device") or info.get("device_name") or ""),
            "mock": bool(info.get("mock")), "error": err}


def _videos(dir_path: str, recursive: bool) -> dict:
    try:
        return video_mod.list_videos(dir_path, recursive=recursive)
    except Exception as e:
        return {"ok": False, "dir": str(dir_path or ""), "videos": [], "error": str(e)}


def _unload(url: str, token: str) -> dict:
    try:
        remote_api.llm_unload(url, token, timeout=None)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def register() -> None:
    try:
        from aiohttp import web
        from server import PromptServer
    except Exception as e:
        print(f"[QwenVL 远程] 路由未注册（{e}）", flush=True)
        return
    routes = getattr(getattr(PromptServer, "instance", None), "routes", None)
    if routes is None:
        print("[QwenVL 远程] 路由未注册（PromptServer.instance 还没就绪）", flush=True)
        return

    @routes.get("/qwenvl_remote/models")
    async def qwenvl_remote_models(request):
        url = (request.query.get("url") or "").strip()
        token = request.query.get("token") or remote_api.default_token()
        return web.json_response(_models(url, token))

    @routes.get("/qwenvl_remote/unload")
    async def qwenvl_remote_unload(request):
        url = (request.query.get("url") or "").strip()
        token = request.query.get("token") or remote_api.default_token()
        return web.json_response(_unload(url, token))

    @routes.get("/qwenvl_remote/videos")
    async def qwenvl_remote_videos(request):
        dir_path = (request.query.get("dir") or "").strip()
        recursive = (request.query.get("recursive") or "").strip() in ("1", "true", "True")
        return web.json_response(_videos(dir_path, recursive))

    print("[QwenVL 远程] 路由已注册：/qwenvl_remote/models、/qwenvl_remote/unload、/qwenvl_remote/videos",
          flush=True)
