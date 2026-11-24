# -*- coding: utf-8 -*-
"""video.py —— 本机视频解码（给 QwenVL 视频加载器用）。

只用本机已有的解码库（ComfyUI 自带环境一般都有）：cv2 优先，decord 兜底。
输出统一为 torch [N,H,W,3] float32(0~1)，可以直接接「QwenVL 远程」的视频/图片输入。
"""

from __future__ import annotations

import os

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".m4v",
              ".mpg", ".mpeg", ".wmv", ".ts", ".m2ts", ".3gp")

MODES = ["均匀取样", "开头连续", "每秒 1 帧"]


def default_dir() -> str:
    """默认扫描 ComfyUI 的 input 目录。"""
    try:
        import folder_paths  # ComfyUI 自带
        return folder_paths.get_input_directory()
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.abspath(os.path.join(here, "..", "..", "input"))
    return cand if os.path.isdir(cand) else here


def _norm_dir(path: str) -> str:
    d = str(path or "").strip().strip('"')
    if not d:
        return default_dir()
    if not os.path.isabs(d):
        d = os.path.join(default_dir(), d)
    return os.path.normpath(d)


def list_videos(dir_path: str = "", recursive: bool = False, limit: int = 800) -> dict:
    """扫描目录下的视频文件，返回 {ok, dir, videos:[{name,path,mb}], error}。"""
    d = _norm_dir(dir_path)
    if not os.path.isdir(d):
        return {"ok": False, "dir": d, "videos": [], "error": f"目录不存在：{d}"}
    items, err = [], ""
    try:
        if recursive:
            walker = ((r, f) for r, _ds, fs in os.walk(d) for f in fs)
        else:
            walker = ((d, f) for f in os.listdir(d))
        for root, name in walker:
            if not name.lower().endswith(VIDEO_EXTS):
                continue
            full = os.path.join(root, name)
            if not os.path.isfile(full):
                continue
            try:
                mb = round(os.path.getsize(full) / 1048576.0, 2)
            except Exception:
                mb = 0.0
            rel = os.path.relpath(full, d).replace("\\", "/")
            items.append({"name": rel, "path": full, "mb": mb})
            if len(items) >= limit:
                err = f"文件太多，只列了前 {limit} 个"
                break
    except Exception as e:
        return {"ok": False, "dir": d, "videos": items, "error": str(e)}
    items.sort(key=lambda x: x["name"].lower())
    return {"ok": True, "dir": d, "videos": items, "error": err}


def resolve_video(dir_path: str = "", name: str = "", custom: str = "") -> str:
    """自定义绝对路径优先，其次目录 + 文件名。"""
    c = str(custom or "").strip().strip('"')
    if c:
        if os.path.isabs(c) and os.path.isfile(c):
            return os.path.normpath(c)
        cand = os.path.join(_norm_dir(dir_path), c)
        if os.path.isfile(cand):
            return os.path.normpath(cand)
        raise RuntimeError(f"找不到视频文件：{c}")
    n = str(name or "").strip()
    if not n:
        raise RuntimeError("「视频文件」是空的：先填目录、点「🔄 刷新视频列表」，或直接填「自定义路径」。")
    cand = n if os.path.isabs(n) else os.path.join(_norm_dir(dir_path), n)
    if not os.path.isfile(cand):
        raise RuntimeError(f"找不到视频文件：{cand}")
    return os.path.normpath(cand)


def _times(count: int, dur: float, start: float, end: float, mode: str) -> list[float]:
    t0 = max(0.0, float(start or 0.0))
    t1 = float(end or 0.0)
    if t1 <= t0:
        t1 = dur if dur > 0 else t0 + 1.0
    t1 = min(t1, dur) if dur > 0 else t1
    span = max(0.001, t1 - t0)
    count = max(1, int(count))
    if mode == "开头连续":
        return [t0 + i / 25.0 for i in range(count)]
    if mode == "每秒 1 帧":
        step = 1.0
        want = min(count, max(1, int(span // step) + 1))
        return [t0 + i * step for i in range(want)]
    if count == 1:
        return [t0 + span * 0.5]
    return [t0 + span * i / float(count - 1) for i in range(count)]


def _resize(frame, max_side: int):
    import cv2
    h, w = frame.shape[:2]
    if max_side and max(h, w) > int(max_side):
        s = float(max_side) / float(max(h, w))
        frame = cv2.resize(frame, (max(2, int(w * s)), max(2, int(h * s))), interpolation=cv2.INTER_AREA)
    return frame


def _decode_cv2(path: str, count: int, start: float, end: float, mode: str, max_side: int):
    import cv2
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError("cv2 打不开这个视频（编码不支持？）")
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0) or 25.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        dur = total / fps if total > 0 else 0.0
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        frames_rgb = []
        if mode == "开头连续":
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(max(0.0, float(start or 0)) * fps))
            while len(frames_rgb) < count:
                ok, fr = cap.read()
                if not ok or fr is None:
                    break
                frames_rgb.append(cv2.cvtColor(_resize(fr, max_side), cv2.COLOR_BGR2RGB))
        else:
            ts = _times(count, dur, start, end, mode)
            for t in ts:
                idx = int(max(0.0, t) * fps)
                if total > 0:
                    idx = min(idx, total - 1)
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ok, fr = cap.read()
                if not ok or fr is None:
                    # 越界就退回从头读
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok, fr = cap.read()
                    if not ok or fr is None:
                        continue
                frames_rgb.append(cv2.cvtColor(_resize(fr, max_side), cv2.COLOR_BGR2RGB))
    finally:
        cap.release()
    meta = {"fps": round(fps, 3), "seconds": round(dur, 2), "width": w, "height": h}
    return frames_rgb, meta


def _decode_decord(path: str, count: int, start: float, end: float, mode: str, max_side: int):
    """兜底：decord 直接给 RGB ndarray，只做缩放。"""
    import cv2
    import decord
    vr = decord.VideoReader(path, ctx=decord.cpu(0))
    n = len(vr)
    if not n:
        return [], {}
    fps = float(vr.get_avg_fps() or 25.0)
    dur = n / fps
    h0, w0 = vr[0].shape[:2]
    ts = _times(count, dur, start, end, mode)
    idxs = sorted({min(n - 1, max(0, int(t * fps))) for t in ts})
    arr = vr.get_batch(idxs).asnumpy()
    frames_rgb = [_resize_rgb(a, max_side) for a in arr]
    meta = {"fps": round(fps, 3), "seconds": round(dur, 2), "width": w0, "height": h0}
    return frames_rgb, meta


def _resize_rgb(a, max_side: int):
    import cv2
    h, w = a.shape[:2]
    if max_side and max(h, w) > int(max_side):
        s = float(max_side) / float(max(h, w))
        a = cv2.resize(a, (max(2, int(w * s)), max(2, int(h * s))), interpolation=cv2.INTER_AREA)
    return a


def load_frames(path: str, count: int = 8, max_side: int = 1024,
                start: float = 0.0, end: float = 0.0, mode: str = "均匀取样"):
    """返回 (torch[N,H,W,3] float 0~1, meta)。"""
    import numpy as np
    import torch

    frames_rgb, meta = [], {}
    errors = []
    for fn in (_decode_cv2, _decode_decord):
        try:
            frames_rgb, meta = fn(path, int(count), float(start or 0), float(end or 0),
                                  str(mode or MODES[0]), int(max_side or 0))
            if frames_rgb:
                break
        except Exception as e:
            errors.append(f"{fn.__name__}: {e}")
    if not frames_rgb:
        raise RuntimeError("视频解码失败（没有读到任何帧）。" + ("；".join(errors) if errors else ""))
    # 统一尺寸（不同帧偶尔会差 1 像素）
    h0, w0 = frames_rgb[0].shape[:2]
    if any(f.shape[:2] != (h0, w0) for f in frames_rgb):
        import cv2
        fixed = [cv2.resize(f, (w0, h0), interpolation=cv2.INTER_AREA)
                 if f.shape[:2] != (h0, w0) else f for f in frames_rgb]
    else:
        fixed = frames_rgb
    arr = np.stack([f.astype(np.float32) / 255.0 for f in fixed], axis=0)
    meta.update({"frames": len(fixed), "width": w0, "height": h0})
    return torch.from_numpy(arr), meta
