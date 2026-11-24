# -*- coding: utf-8 -*-
"""nodes.py —— QwenVL 全远程版节点（本机不加载任何权重，推理全在 RemoteCLIPVAE 服务端）。

节点清单：
    QwenVL 远程加载器   QwenVLRemoteLoader      -> 远程模型句柄（带「🔄 刷新模型列表」）
    QwenVL 视频加载器   QwenVLVideoLoader       -> 本机视频抽帧成 IMAGE（带「🔄 刷新视频列表」）
    QwenVL 远程         QwenVLRemote            -> 图生文 / 反推（图片/视频都行）
    QwenVL 远程（高级） QwenVLRemoteAdvanced    -> 多了温度、上下文长度
    QwenVL 提示词增强    QwenVLRemotePromptEnhancer
    QwenVL 文本净化      QwenVLRemoteCleaner    -> 纯本地文本处理，不调模型
"""

from __future__ import annotations

import os
import re

from . import remote_api
from . import video as video_mod
from .imaging import tensor_to_png_frames

HANDLE = "QWENVL_REMOTE"
CATEGORY = "🧪AILab/QwenVL 远程"

NO_MODEL = "（服务端没有可用的 GGUF 提示词模型）"
FAIL_HINT = "（连不上服务端：先填地址再点「🔄 刷新模型列表」）"
NO_VISION = "（不使用 / 纯文本）"

LANGS = ["跟随提示词", "中文", "英文"]

PRESETS = {
    "🖼️ 简短描述": "用一两句自然语言概括这张图的主体与场景。",
    "🖼️ 详细描述": ("详细描述这张图：主体与外观、服饰与配饰、动作与姿态、环境与背景、"
                     "光线与阴影、构图与机位、镜头与景深、色彩与材质、整体氛围。"),
    "🖼️ 超详细描述": ("分段落把这张图写透：① 主体（身份、年龄感、外貌、服饰面料、配饰细节）"
                       "② 动作与表情 ③ 环境与道具 ④ 光线方向与质感 ⑤ 机位/焦段/景深 ⑥ 色彩与影调 ⑦ 氛围。"),
    "🏷️ 关键词标签": "只输出英文关键词标签，用英文逗号分隔，不要句子、不要解释。",
    "🎬 电影感描述": "以电影摄影的视角描述：机位与运动、焦段与景深、光比与色温、色调风格、胶片质感。",
    "🔍 画面分析": "分析画面的构成：视觉重心、引导线、光影与色彩的用意，以及它传达的情绪。",
    "📹 视频概要": "按时间顺序描述这段视频里发生了什么，只写看到的内容。",
    "📖 短故事": "根据画面写一段 150 字以内的短故事，有具体的人/物/动作，不要空泛抒情。",
    "🪄 提示词扩写": ("把画面转写成一条可直接用于文生图的高质量提示词：先正向（主体、动作、服饰、"
                      "环境、光线、镜头、风格），再单独给一行负向提示词。"),
}

STYLES = ["摄影写实", "电影质感", "插画/动漫", "3D 渲染", "产品广告", "建筑室内"]
STYLE_RULES = {
    "摄影写实": "写实摄影：真实材质、自然光、合理景深，避免塑料感与过度锐化。",
    "电影质感": "电影剧照感：明确机位与焦段、强光比、胶片色调、细微颗粒。",
    "插画/动漫": "插画/动漫风：干净的线条与色块、明确的风格化光影，不要照片感。",
    "3D 渲染": "3D 渲染：干净材质、PBR 质感、体积光与反射细节，构图居中稳定。",
    "产品广告": "商业广告：主体突出、棚拍布光、干净背景、突出材质卖点。",
    "建筑室内": "建筑/室内：强调透视、空间层次、材质与灯光设计，避免畸变。",
}


# --------------------------------------------------------------------------- #
# 公共工具
# --------------------------------------------------------------------------- #
def _combo(kind: str, url: str) -> list:
    names, err = remote_api.list_models(kind, url, remote_api.default_token(), timeout=2.0)
    if names:
        return names
    return [FAIL_HINT if err and "无法连接" in err else NO_MODEL]


def _tidy(name: str) -> str:
    return "" if (not name or str(name).startswith(("（", "("))) else str(name).strip()


_CJK = re.compile(r"[\u4e00-\u9fff]")


def _mostly_not_chinese(text: str) -> bool:
    """中文模式下判断输出是否「没照做」：一个汉字都没有，或英文字母明显压过汉字。"""
    s = str(text or "")
    cjk = len(_CJK.findall(s))
    latin = len(re.findall(r"[A-Za-z]", s))
    if cjk == 0 and latin == 0:
        return False  # 纯数字/符号，不算违规
    return cjk == 0 or latin > cjk * 1.5


def _resolve_lang(lang: str, sample: str = "") -> str:
    """把「中文 / 英文 / 跟随提示词」落成实际语言：中文 / 英文 / 空（不限制）。"""
    lang = str(lang or "").strip()
    if lang == "中文":
        return "中文"
    if lang == "英文":
        return "英文"
    if lang == "跟随提示词":
        return "中文" if (sample and _CJK.search(sample)) else "英文"
    return ""


def _lang_rule(lang: str, sample: str = "") -> str:
    """强制语言规则：中文/英文各一句硬要求，放到 system 末尾也放到用户内容末尾。"""
    real = _resolve_lang(lang, sample)
    if real == "中文":
        return ("输出语言：简体中文（强制）。整段回答必须用简体中文书写，禁止英文句子、英文短语、"
                "英文标签或中英混排；只有品牌名、模型名、人名等确实无法翻译的专有名词可保留原文。")
    if real == "英文":
        return "输出语言：英文（强制）。Write the whole answer in English, no Chinese characters."
    return ""


MAX_IMAGES = 10


def _image_slots(n: int = MAX_IMAGES) -> dict:
    """图片1…图片10 十个可选输入；界面（web/dynamic_images.js）只露出「已连的 + 下面一个新口」。"""
    return {f"图片{i}": ("IMAGE",) for i in range(1, n + 1)}


def _collect_images(kw: dict) -> list:
    out = []
    for i in range(1, MAX_IMAGES + 1):
        v = kw.get(f"图片{i}")
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            out.extend([x for x in v if x is not None])
        else:
            out.append(v)
    video = kw.get("视频")
    if video is not None:
        try:
            frames = tensor_to_png_frames(video)
            if len(frames) > 4:
                step = max(1, len(frames) // 4)
                frames = frames[::step][:4]
            out.extend(frames)
        except Exception as e:
            print(f"[QwenVL 远程] 视频取帧失败：{e}", flush=True)
    return out


def _chat(handle: dict, text: str, system: str, images: list, max_tokens: int,
          temperature: float = 0.7, n_ctx: int = 0, seed: int = 0, lang: str = "") -> tuple[str, str]:
    if not isinstance(handle, dict) or not handle.get("model"):
        raise RuntimeError("「远程模型」输入要接「QwenVL 远程加载器」的输出。")
    url = handle.get("url") or remote_api.default_url()
    model = handle["model"]
    mmproj = "" if str(handle.get("mmproj") or "").startswith(("（", "(")) else str(handle.get("mmproj") or "")
    n_ctx = int(n_ctx or handle.get("n_ctx") or 4096)
    if lang == "中文":
        # 把语言要求同时压到用户内容末尾（贴得越近越管用，模型容易只看最后一句）
        text = text.rstrip() + "\n\n（要求：以上全部内容必须用简体中文输出，不要出现英文短语或英文句子。）"
    elif lang == "英文":
        text = text.rstrip() + "\n\n(Requirement: write the whole answer in English only.)"
    frames = [f for f in images if isinstance(f, str)] if images and isinstance(images[0], str) else images
    # 图片项如果还是张量（视频帧之外的路径），这里统一转 PNG
    frames = [f if isinstance(f, str) else tensor_to_png_frames(f)[0] for f in frames]
    if frames and not mmproj:
        raise RuntimeError("这次带了参考图，但加载器里的「视觉模型」是「不使用 / 纯文本」："
                           "在服务端选一个 mmproj 视觉模型再刷新。")
    data = remote_api.llm_chat(url, model, text, system=system, images=frames, mmproj=mmproj,
                              max_tokens=max_tokens, temperature=temperature, seed=seed,
                              n_ctx=n_ctx, timeout=None)
    out = str(data.get("text") or "")
    tokens = data.get("tokens", "?")
    seconds = data.get("seconds", "?")
    need_fix = ""
    if lang == "中文" and out and _mostly_not_chinese(out):
        need_fix = "中文"
    elif lang == "英文" and out:
        if len(_CJK.findall(out)) > len(re.findall(r"[A-Za-z]", out)):
            need_fix = "英文"
    if need_fix:
        # 模型无视语言要求（Qwen-Image 系提示词模型尤其顽固）：再发一次纯文本翻译请求兜底
        print(f"[QwenVL 远程] 输出不是{need_fix}，追加一次强制翻译", flush=True)
        if need_fix == "中文":
            ask = "把下面这段内容翻译成简体中文，只输出译文，不要解释、不要加标题：\n\n" + out
            sys2 = "你是翻译，只输出简体中文译文。"
        else:
            ask = "Translate the following into English. Output the translation only:\n\n" + out
            sys2 = "You are a translator. Output English only."
        try:
            fix = remote_api.llm_chat(
                url, model, ask, system=sys2, images=[], mmproj="", max_tokens=max_tokens,
                temperature=0.2, seed=seed, n_ctx=n_ctx, timeout=None)
            got = str((fix or {}).get("text") or "").strip()
            if got and (_CJK.search(got) if need_fix == "中文" else re.search(r"[A-Za-z]", got)):
                out = got
                tokens = fix.get("tokens", tokens)
                seconds = fix.get("seconds", seconds)
        except Exception as e:
            print(f"[QwenVL 远程] 语言兜底翻译失败：{e}", flush=True)
    info = (f"{model}{' + ' + mmproj if mmproj else ''} | 图 {len(frames)} 张 | "
            f"{tokens} tok | {data.get('tps', '?')} tok/s | {seconds}s")
    if handle.get("unload_after"):
        try:
            remote_api.llm_unload(url, remote_api.default_token(), timeout=None)
        except Exception as e:
            print(f"[QwenVL 远程] 卸载失败：{e}", flush=True)
    if not out:
        raise RuntimeError(f"服务端没回文本（{info}）：看服务端日志那行报错")
    print(f"[QwenVL 远程] {info}", flush=True)
    return out, info


# --------------------------------------------------------------------------- #
# 1) 远程加载器（模型清单 + 刷新按钮）
# --------------------------------------------------------------------------- #
class QwenVLRemoteLoader:
    """选服务端上的模型，输出一个句柄给下游节点用。模型本身不下载到本机。"""

    @classmethod
    def INPUT_TYPES(cls):
        url = remote_api.default_url()
        models = _combo("llm", url)
        visions = _combo("llm_vision", url)
        return {"required": {
            "服务端地址": ("STRING", {"default": url,
                                    "tooltip": "RemoteCLIPVAE 服务端地址，例如 http://10.10.10.2:8199"}),
            "服务端显卡": ("STRING", {"default": "", "tooltip": "只读：点「🔄 刷新模型列表」时自动填"}),
            "模型": (models, {"default": models[0],
                            "tooltip": "服务端 models/LLM 里的 gguf；清单由刷新按钮从服务端拉"}),
            "视觉模型": ([NO_VISION] + [v for v in visions if not v.startswith(("（", "("))],
                      {"default": NO_VISION, "tooltip": "要传图必须选一个 mmproj"}),
            "上下文长度": ("INT", {"default": 4096, "min": 512, "max": 32768, "step": 128}),
            "卸载模型": (["保持加载", "跑完卸载"], {"default": "保持加载",
                                             "tooltip": "跑完卸载=每次生成完就把服务端 LLM 退出显存"}),
        }}

    RETURN_TYPES = (HANDLE, "STRING")
    RETURN_NAMES = ("远程模型", "状态")
    FUNCTION = "build"
    CATEGORY = CATEGORY
    DESCRIPTION = ("把「服务端上的 QwenVL 模型」打包成句柄传给下游。本机不加载任何权重，"
                   "点「🔄 刷新模型列表」可从服务端拉最新清单。")

    @classmethod
    def VALIDATE_INPUTS(cls, **kwargs):
        return True

    def build(self, 服务端地址, 服务端显卡, 模型, 视觉模型, 上下文长度, 卸载模型):
        url = (str(服务端地址 or "").strip() or remote_api.default_url())
        model = _tidy(模型)
        if not model:
            raise RuntimeError("没有可用的远程模型：先确认服务端在跑、models/LLM 里有 gguf，"
                               "再把「服务端地址」填对并点「🔄 刷新模型列表」。")
        mmproj = "" if str(视觉模型 or "").startswith(("（", "(")) else str(视觉模型).strip()
        handle = {"url": url, "model": model, "mmproj": mmproj,
                  "n_ctx": int(上下文长度), "unload_after": str(卸载模型) == "跑完卸载"}
        info = f"{model}{' + ' + mmproj if mmproj else '（纯文本）'}"
        print(f"[QwenVL 远程] 加载器就绪：{info} @ {url}", flush=True)
        return (handle, info)


# --------------------------------------------------------------------------- #
# 1.5) 视频加载器（本机解码成帧，喂给「QwenVL 远程」反推视频）
# --------------------------------------------------------------------------- #
class QwenVLVideoLoader:
    """读本机视频文件，按指定方式抽帧成 IMAGE 批次，直接接「QwenVL 远程」的 视频 / 图片 输入。"""

    @classmethod
    def INPUT_TYPES(cls):
        d = video_mod.default_dir()
        got = video_mod.list_videos(d, limit=300)
        names = [v["name"] for v in got.get("videos") or []]
        if not names:
            names = ["（目录里没有视频，填路径后点 🔄 刷新视频列表）"]
        return {"required": {
            "视频目录": ("STRING", {"default": d,
                                "tooltip": "留空=ComfyUI 的 input 目录；可填绝对路径或相对 input 的路径"}),
            "视频文件": (names, {"default": names[0],
                            "tooltip": "由「🔄 刷新视频列表」从「视频目录」扫描得到"}),
            "自定义路径": ("STRING", {"default": "",
                                 "tooltip": "填了就用它（绝对路径，或相对「视频目录」），优先于「视频文件」"}),
            "取帧数量": ("INT", {"default": 8, "min": 1, "max": 32,
                             "tooltip": "「视频」输入在 QwenVL 远程里只会再抽 4 帧；要多帧就接到 图片1 口"}),
            "取帧方式": (video_mod.MODES, {"default": video_mod.MODES[0]}),
            "起始秒": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 86400.0, "step": 0.1}),
            "结束秒": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 86400.0, "step": 0.1,
                             "tooltip": "0=到视频结尾"}),
            "最长边": ("INT", {"default": 1024, "min": 0, "max": 4096, "step": 64,
                           "tooltip": "抽帧后最长边缩到这个值；0=原尺寸"}),
        }}

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("图像", "状态")
    FUNCTION = "run"
    CATEGORY = CATEGORY
    DESCRIPTION = ("加载本机视频并按「均匀取样 / 开头连续 / 每秒 1 帧」抽帧，输出 IMAGE 批次，"
                   "接到「QwenVL 远程」的 视频 或 图片1 口即可反推视频内容。")

    @classmethod
    def VALIDATE_INPUTS(cls, **kwargs):
        return True

    def run(self, 视频目录, 视频文件, 自定义路径, 取帧数量, 取帧方式, 起始秒, 结束秒, 最长边):
        path = video_mod.resolve_video(视频目录, 视频文件, 自定义路径)
        frames, meta = video_mod.load_frames(path, int(取帧数量), int(最长边),
                                            float(起始秒 or 0), float(结束秒 or 0), str(取帧方式))
        name = os.path.basename(path)
        info = (f"{name} | {meta.get('seconds', 0)}s | {meta.get('width')}x{meta.get('height')} | "
                f"{meta.get('frames', 0)} 帧 | 最长边 {最长边}")
        print(f"[QwenVL 视频] 已抽帧：{info}", flush=True)
        return (frames, info)


# --------------------------------------------------------------------------- #
# 2) 图生文（简单）
# --------------------------------------------------------------------------- #
class QwenVLRemote:
    """把图/视频发到服务端，让 QwenVL 写描述或反推提示词。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "远程模型": (HANDLE, {"forceInput": True}),
            "预设提示词": (list(PRESETS.keys()), {"default": "🖼️ 详细描述"}),
            "用户提示词": ("STRING", {"default": "", "multiline": True,
                                  "tooltip": "留空=只用预设；有图无词时按预设反推"}),
            "输出语言": (LANGS, {"default": "跟随提示词"}),
            "最大输出长度": ("INT", {"default": 768, "min": 32, "max": 4096}),
            "种子值": ("INT", {"default": 0, "min": 0, "max": 0xffffffff}),
        }, "optional": {
            "视频": ("IMAGE",),
            **_image_slots(),
        }}

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("RESPONSE", "状态")
    FUNCTION = "run"
    CATEGORY = CATEGORY
    DESCRIPTION = "QwenVL 远程版：图片在服务端推理，本机只收文本。"

    @classmethod
    def VALIDATE_INPUTS(cls, **kwargs):
        return True

    def run(self, 远程模型, 预设提示词, 用户提示词, 输出语言, 最大输出长度, 种子值, **kw):
        text = (PRESETS.get(预设提示词, 预设提示词) + "\n" + str(用户提示词 or "").strip()).strip()
        system = "你是 QwenVL 视觉助手，只输出用户要的内容，不要寒暄与思考过程。" + _lang_rule(输出语言, str(用户提示词 or ""))
        images = _collect_images(kw)
        lang = _resolve_lang(输出语言, str(用户提示词 or ""))
        out, info = _chat(远程模型, text, system, images, int(最大输出长度), seed=int(种子值), lang=lang)
        return (out, info)


# --------------------------------------------------------------------------- #
# 3) 图生文（高级）
# --------------------------------------------------------------------------- #
class QwenVLRemoteAdvanced(QwenVLRemote):
    """多了系统提示词、温度、上下文长度。"""

    @classmethod
    def INPUT_TYPES(cls):
        base = super().INPUT_TYPES()
        req = dict(base["required"])
        req.pop("种子值")
        req.update({
            "系统提示词": ("STRING", {"default": "", "multiline": True,
                                  "tooltip": "留空=用内置的视觉助手提示词"}),
            "温度": ("FLOAT", {"default": 0.7, "min": 0.0, "max": 2.0, "step": 0.05}),
            "上下文长度": ("INT", {"default": 0, "min": 0, "max": 32768, "step": 128,
                              "tooltip": "0=跟随加载器"}),
            "种子值": ("INT", {"default": 0, "min": 0, "max": 0xffffffff}),
        })
        return {"required": req, "optional": base["optional"]}

    def run(self, 远程模型, 预设提示词, 用户提示词, 输出语言, 最大输出长度, 系统提示词,
            温度, 上下文长度, 种子值, **kw):
        text = (PRESETS.get(预设提示词, 预设提示词) + "\n" + str(用户提示词 or "").strip()).strip()
        lang = _resolve_lang(输出语言, str(用户提示词 or ""))
        system = str(系统提示词 or "").strip() \
            or ("你是 QwenVL 视觉助手，只输出用户要的内容，不要寒暄与思考过程。" + _lang_rule(输出语言,
                                                                                      str(用户提示词 or "")))
        images = _collect_images(kw)
        out, info = _chat(远程模型, text, system, images, int(最大输出长度),
                          temperature=float(温度), n_ctx=int(上下文长度), seed=int(种子值), lang=lang)
        return (out, info)


# --------------------------------------------------------------------------- #
# 4) 提示词增强
# --------------------------------------------------------------------------- #
class QwenVLRemotePromptEnhancer:
    """把一句想法扩写成可用的出图提示词（服务端跑）。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "远程模型": (HANDLE, {"forceInput": True}),
            "用户想法": ("STRING", {"default": "", "multiline": True}),
            "风格": (STYLES, {"default": STYLES[0]}),
            "输出语言": (LANGS, {"default": "英文"}),
            "最大输出长度": ("INT", {"default": 512, "min": 32, "max": 2048}),
            "参考图最大像素": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 4.0, "step": 0.1,
                                   "tooltip": "接参考图时先缩到这个百万像素上限"}),
        }, "optional": {
            **_image_slots(),
        }}

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("提示词", "状态")
    FUNCTION = "run"
    CATEGORY = CATEGORY

    @classmethod
    def VALIDATE_INPUTS(cls, **kwargs):
        return True

    def run(self, 远程模型, 用户想法, 风格, 输出语言, 最大输出长度, 参考图最大像素, **kw):
        idea = str(用户想法 or "").strip()
        if not idea and not any(kw.get(f"图片{i}") is not None for i in range(1, MAX_IMAGES + 1)):
            raise RuntimeError("「用户想法」为空且没有接参考图：至少给一样。")
        system = ("你是出图提示词工程师。把用户的想法扩写成一条可直接用于文生图/图生图的提示词："
                  "正向写清主体、动作、服饰、环境、光线、镜头、风格；再另起一行给负向提示词。"
                  f"\n风格要求：{STYLE_RULES.get(风格, '')}"
                  "\n只输出提示词正文，不要解释、不要标题、不要思考过程。" + _lang_rule(输出语言, idea))
        frames = []
        for i in range(1, MAX_IMAGES + 1):
            v = kw.get(f"图片{i}")
            if v is None:
                continue
            if isinstance(v, str):
                frames.append(v)
            elif isinstance(v, (list, tuple)):
                frames.extend([x if isinstance(x, str) else tensor_to_png_frames(
                    x, max_pixels=int(float(参考图最大像素 or 0) * 1_000_000))[0] for x in v])
            else:
                frames.extend(tensor_to_png_frames(v, max_pixels=int(float(参考图最大像素 or 0) * 1_000_000)))
        text = idea or "参考这张图，写出可复用的出图提示词。"
        lang = _resolve_lang(输出语言, idea)
        out, info = _chat(远程模型, text, system, frames, int(最大输出长度), lang=lang)
        return (out, info)


# --------------------------------------------------------------------------- #
# 5) 文本净化（不调模型）
# --------------------------------------------------------------------------- #
class QwenVLRemoteCleaner:
    """把模型输出的各种包装去掉：```代码块、首尾引号、编号、多余空行。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "文本": ("STRING", {"default": "", "multiline": True}),
            "去掉代码块": ("BOOLEAN", {"default": True}),
            "去掉首尾引号": ("BOOLEAN", {"default": True}),
            "压缩空行": ("BOOLEAN", {"default": True}),
            "去掉行首编号": ("BOOLEAN", {"default": False}),
        }}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("文本",)
    FUNCTION = "run"
    CATEGORY = CATEGORY

    @classmethod
    def VALIDATE_INPUTS(cls, **kwargs):
        return True

    def run(self, 文本, 去掉代码块, 去掉首尾引号, 压缩空行, 去掉行首编号):
        s = str(文本 or "")
        if 去掉代码块:
            s = re.sub(r"```[a-zA-Z0-9_-]*\s*", "", s).replace("```", "")
        if 去掉首尾引号:
            s = s.strip()
            for a, b in (('"', '"'), ("'", "'"), ("“", "”"), ("「", "」"), ("『", "』")):
                if len(s) >= 2 and s.startswith(a) and s.endswith(b):
                    s = s[1:-1].strip()
        if 去掉行首编号:
            s = re.sub(r"(?m)^\s*(?:\d+[.、)]|[-*•])\s*", "", s)
        if 压缩空行:
            s = re.sub(r"\n{3,}", "\n\n", s).strip()
        return (s,)


NODE_CLASS_MAPPINGS = {
    "QwenVLRemoteLoader": QwenVLRemoteLoader,
    "QwenVLVideoLoader": QwenVLVideoLoader,
    "QwenVLRemote": QwenVLRemote,
    "QwenVLRemoteAdvanced": QwenVLRemoteAdvanced,
    "QwenVLRemotePromptEnhancer": QwenVLRemotePromptEnhancer,
    "QwenVLRemoteCleaner": QwenVLRemoteCleaner,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "QwenVLRemoteLoader": "QwenVL 远程加载器 (QwenVL Remote Loader)",
    "QwenVLVideoLoader": "QwenVL 视频加载器 (QwenVL Video Loader)",
    "QwenVLRemote": "QwenVL 远程 (QwenVL Remote)",
    "QwenVLRemoteAdvanced": "QwenVL 远程·高级 (QwenVL Remote Advanced)",
    "QwenVLRemotePromptEnhancer": "QwenVL 提示词增强 (QwenVL Remote Prompt Enhancer)",
    "QwenVLRemoteCleaner": "QwenVL 文本净化 (Text Cleaner)",
}
