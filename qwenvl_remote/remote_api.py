# -*- coding: utf-8 -*-
"""remote_api.py —— 访问 RemoteCLIPVAE 服务端的轻量客户端（只用标准库）。

协议（和服务端 model_server 一致）：
    GET  /health           -> {"ok": true, "data": {..., "models": {...}, "device": "...", ...}}
    POST /rpc {"op","params"} -> {"ok": true, "data": <pack>}
本插件只用到 llm / llm_vision 两类模型与 llm_chat、llm_unload 两个操作。
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request

from .wire import pack, unpack

_DIR = os.path.dirname(os.path.abspath(__file__))
_CFG = os.path.join(_DIR, "remote_server.json")
_FALLBACK = "http://127.0.0.1:8199"


def config() -> dict:
    """插件目录里的 remote_server.json：{"url": "...", "token": "..."}"""
    url, token = _FALLBACK, ""
    try:
        with open(_CFG, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        url = str(d.get("url") or url).strip() or url
        token = str(d.get("token") or "")
    except Exception:
        pass
    env = os.environ.get("REMOTE_CLIPVAE_SERVER")
    if env:
        url = env.strip()
    env_t = os.environ.get("REMOTE_CLIPVAE_TOKEN")
    if env_t is not None:
        token = env_t
    return {"url": url.rstrip("/"), "token": token}


def default_url() -> str:
    return config()["url"]


def default_token() -> str:
    return config()["token"]


def base_url(url: str) -> str:
    url = (url or "").strip() or default_url()
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "http://" + url
    tail = url.split("//", 1)[-1]
    if ":" not in tail:
        url = url + ":8199"
    return url.rstrip("/")


def _headers(token: str) -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        h["X-Auth-Token"] = token
    return h


def _short(err: str, limit: int = 300) -> str:
    err = (err or "").strip()
    return err if len(err) <= limit else err[:limit] + " ..."


def health(url: str = "", token: str = "", timeout: float | None = 6.0) -> dict:
    base = base_url(url)
    req = urllib.request.Request(base + "/health", headers=_headers(token or default_token()))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"这个地址回的是 HTTP {e.code}，不像 RemoteCLIPVAE 服务端（/health 应返回 ok=true）") from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        raise RuntimeError(f"无法连接服务端 {base}：{getattr(e, 'reason', e)}") from None
    if not payload.get("ok"):
        raise RuntimeError(str(payload.get("error") or "服务端返回 ok=false"))
    return payload.get("data") or {}


def rpc(url: str, op: str, params: dict | None = None, token: str = "", timeout: float | None = None):
    base = base_url(url)
    body = json.dumps({"op": op, "params": pack(params or {})}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base + "/rpc", data=body, method="POST",
                                 headers=_headers(token or default_token()))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raw = e.read() or b"{}"
        try:
            err = json.loads(raw.decode("utf-8", "replace")).get("error", "")
        except Exception:
            err = raw.decode("utf-8", "replace")
        raise RuntimeError(f"【服务端】{op} 失败（HTTP {e.code}）：{_short(err)}") from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        raise RuntimeError(
            f"无法连接服务端 {base}：{getattr(e, 'reason', e)}。\n"
            f"排查：① 服务端在跑吗；② 同一网段吗；③ 防火墙放行端口吗；④ 地址/端口填对了吗") from None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        raise RuntimeError(f"【服务端】{op} 返回的不是合法 JSON：{_short(raw.decode('utf-8', 'replace'), 200)}") from None
    if not payload.get("ok"):
        raise RuntimeError(f"【服务端】{op} 失败：{_short(str(payload.get('error') or '未知错误'))}")
    return unpack(payload.get("data"))


def list_models(kind: str, url: str = "", token: str = "", timeout: float = 4.0) -> tuple[list, str]:
    """返回 (名字列表, 错误文案)。连不上就给空列表 + 原因，不抛异常。"""
    try:
        data = health(url, token, timeout)
    except Exception as e:
        return [], _short(str(e), 160)
    got = ((data.get("models") or {}).get(kind) or {})
    if not got:
        return [], f"服务端 models.{kind} 是空的"
    return sorted(got.keys()), ""


def device_name(url: str = "", token: str = "") -> str:
    try:
        d = health(url, token, 3.0)
        return str(d.get("device") or d.get("device_name") or "")
    except Exception:
        return ""


def llm_unload(url: str = "", token: str = "", timeout: float | None = None):
    return rpc(url, "llm_unload", {}, token=token, timeout=timeout)


def llm_chat(url: str, model: str, text: str, system: str = "", images: list | None = None,
             mmproj: str = "", max_tokens: int = 512, temperature: float = 0.7,
             top_p: float = 0.9, repeat_penalty: float = 1.1, seed=None,
             n_ctx: int = 4096, timeout: float | None = None) -> dict:
    params = {"model": model, "mmproj": mmproj, "text": text, "system": system,
              "images": list(images or []), "max_tokens": int(max_tokens),
              "temperature": float(temperature), "top_p": float(top_p),
              "repeat_penalty": float(repeat_penalty), "n_ctx": int(n_ctx), "auto_load": True}
    if seed:
        params["seed"] = int(seed)
    return rpc(url, "llm_chat", params, timeout=timeout) or {}
