# ComfyUI-QwenVL-Remote

把原版 **ComfyUI-QwenVL** 换一份「模型全在远程」的版本：本机不下载、不加载任何 QwenVL 权重，
只发图和文字，推理跑在 **RemoteCLIPVAE 服务端**（默认 8199）上，拿回文本。

## 节点

| 节点 | 作用 |
| --- | --- |
| **QwenVL 远程加载器 (QwenVL Remote Loader)** | 选服务端上的提示词模型 + 视觉模型（mmproj），输出句柄给下游。带 **🔄 刷新模型列表**、**🗑 卸载服务端模型** 两个按钮，以及只读的「服务端显卡」 |
| **QwenVL 视频加载器 (QwenVL Video Loader)** | 本机读视频（mp4/mov/mkv/webm/avi/flv…），按「均匀取样 / 开头连续 / 每秒 1 帧」抽帧，输出 IMAGE 批次 + 状态。带 **🔄 刷新视频列表**，扫描目录里的视频文件 |
| **QwenVL 远程 (QwenVL Remote)** | 图生文 / 反推：预设提示词 9 种 + 用户提示词 + 输出语言 + 最大输出长度 + 种子；图片输入**动态增长，最多 10 个**；另有 视频（取 4 帧） |
| **QwenVL 远程·高级 (QwenVL Remote Advanced)** | 同上，多了系统提示词、温度、上下文长度 |
| **QwenVL 提示词增强 (QwenVL Remote Prompt Enhancer)** | 一句想法 → 可直接出图的提示词（摄影写实 / 电影质感 / 插画 / 3D / 产品 / 建筑 六种风格）；参考图同样动态、最多 10 个 |
| **QwenVL 文本净化 (Text Cleaner)** | 纯本地处理：去 ``` 代码块、首尾引号、行首编号、压缩空行 |

原版里「QwenVL / QwenVL (Advanced) / QwenVL (GGUF) / QwenVL Advanced (GGUF) / Prompt Enhancer / GGUF Prompt Enhancer」
这些要本机加载权重的节点，在这一份里全部合并成上面这几个远程节点；`HuggingFace Downloader`
（下载本机权重）在远程模式下不需要，因此没有做。

## 反推视频

`QwenVL 视频加载器` → `QwenVL 远程`：

1. 「视频目录」默认 ComfyUI 的 `input`，可以填绝对路径；点 **🔄 刷新视频列表**，下拉里就是目录内的视频。
2. 也可以直接在「自定义路径」填完整路径（优先于下拉选择）。
3. 「取帧数量」决定吐出多少帧；「取帧方式」均匀取样 / 开头连续 / 每秒 1 帧；「起始秒 / 结束秒」截取区间（结束 0 = 到结尾）。
4. 接线两种：
   - 接 `QwenVL 远程` 的 **视频** 口 → 节点内部会从批次里再均匀抽 **4 帧**发给模型（提示词选 `📹 视频概要`）；
   - 接 **图片1** 口 → **整批帧都会发出去**（想让它看更多帧就用这个，配合 `📹 视频概要` 或 `🖼️ 超详细描述`）。
5. 「最长边」把每帧缩到该尺寸（默认 1024，0 = 原尺寸），控制上传体积与推理耗时。

解码用本机已有的 `cv2`（ComfyUI 自带环境一般都有），失败自动回退 `decord`；全程在本机，不占服务端显存。

## 输出语言（强制）

「输出语言」选中文/英文时不是只写一句软提示，而是三重约束：

1. `system` 里写硬要求（中文：禁止英文句子、英文短语、英文标签与中英混排）；
2. 用户内容末尾再压一遍同一要求（模型往往只看最后一段）；
3. 拿到结果后做语言检测：中文模式下汉字数为 0、或英文字母明显压过汉字（> 1.5 倍），
   自动追加一次纯文本翻译请求把结果翻成简体中文再用（英文模式对称处理）；
   控制台会打 `[QwenVL 远程] 输出不是中文，追加一次强制翻译`。

「跟随提示词」则按用户提示词里有没有汉字决定中文还是英文。

## 动态图片输入

`QwenVLRemote` / `QwenVLRemoteAdvanced` / `QwenVLRemotePromptEnhancer` 三个节点的图片口由
`web/dynamic_images.js` 管理：后端固定声明 `图片1`…`图片10`，前端只露出「已连线的 + 下面一个新口」。
连上第 1 张 → 下面自动出现 `图片2`；再连 → `图片3`……到 `图片10` 封顶；断开连线会自动收回去。
空口不参与推理，服务端只收到真正接了图的张数。

## 依赖与配置

1. **服务端**：RemoteCLIPVAE 服务端 exe 在跑（`GET /health` 能回 `ok:true`），`models/LLM` 里至少一个 gguf；
   要传图还要有 mmproj（文件名带 `mmproj` / `vision` 会被识别成视觉模型）。
2. **本机**：不需要装 transformers / llama-cpp，也不需要显卡；只有 ComfyUI 本体。
3. `remote_server.json`（本插件目录，首次启动自动生成）：

```json
{ "url": "http://10.10.10.2:8199", "token": "" }
```

也可以在节点的「服务端地址」里直接改，改完会自动重新拉清单。

## 协议

```
GET  /health                     取 models.llm / models.llm_vision / device
POST /rpc  {"op":"llm_chat"}      text + images(PNG base64) + system + max_tokens + temperature + n_ctx
POST /rpc  {"op":"llm_unload"}    让服务端把 LLM 退出显存
```

浏览器跨域会被挡，所以前端按钮走本机代理：`GET /qwenvl_remote/models`、`GET /qwenvl_remote/unload`（见 `api.py`）。

## 目录

```
ComfyUI-QwenVL-Remote/
├── __init__.py          # 注册节点 + 路由 + 默认配置
├── nodes.py             # 5 个节点
├── remote_api.py        # /health、/rpc 客户端（标准库）
├── wire.py              # 与服务端 codec 对齐的最小 pack/unpack
├── imaging.py           # IMAGE 张量 -> PNG base64
├── api.py               # /qwenvl_remote/* 本机代理
├── web/refresh.js       # 刷新按钮、只读显卡栏、卸载按钮
└── README.md
```

安装：把整个 `ComfyUI-QwenVL-Remote` 文件夹放进 `ComfyUI/custom_nodes/`，重启 ComfyUI。
