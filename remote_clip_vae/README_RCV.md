## v1.7.71 变更

- 移除节点 `RemoteGGUFUNETLoader`（远程 GGUF 模型加载器）与 `RemoteKSampler`（远程 K 采样器）
- 同步删掉前端刷新映射与 `REMOTE_MODEL_TYPE` 常量；插件对外 11 个节点
- 服务端接口 `unet_load_gguf` / `unet_sample` 保留，仅是前端不再提供入口
## v1.7.70 变更

- 加载扩散模型 GGUF 前读头部元信息（arch / name / 张量数）并打到服务端日志
- 结构认不出时错误信息给出 arch、张量数与键名分布，并按 arch 判断是文本编码器还是未知 DiT
- 键名多带 `model.` / `transformer.` / `net.` / `diffusion_model.` 前缀时自动剥掉重试
- 拦截名单补充 llama.cpp 键名（`blk.*`、`token_embd`、`output_norm`）
## v1.7.69 变更

- 新增 `unet_gguf` 模型类别：递归扫服务端 `models/diffusion_models` / `models/unet` / `models/GGUF`（含模型根目录下的同名子目录），「远程 GGUF 模型加载器」的下拉改用它；老服务端无此类别时自动退回 llm 目录
- 名字解析支持相对路径 / 纯文件名 / 绝对路径；缓存键改为绝对路径，同一文件不会重复加载
- `probe` 白名单加 `unet_gguf`，空清单时提示把 gguf 放到 diffusion_models / unet 下
## v1.7.68 修复

- 服务端「GGUF 当扩散模型加载」误用了文本模型读取器（`gguf_clip_loader`）→ 报 `Unexpected text model architecture type: 'qwen35'`；改为 `gguf_sd_loader`，文本编码器仍走 `gguf_clip_loader`
- 扩散模型加载前加结构判定：只有 llama 层、没有 DiT/UNet 块的 GGUF（如 `Qwen-Image-2.1-PE`，arch=qwen35）直接拦下并说明「它只该当 LLM 用」
## v1.7.67 修复

- 「装了 ComfyUI-GGUF 还是报 weights_only」：服务端 exe 不加载 custom_nodes，`sd.load_clip` 走 torch.load 读不了 gguf → 改为自己找 custom_nodes 里的 GGUF 包，借 `GGMLOps` + `gguf_clip_loader`，走 `load_text_encoder_state_dicts` / `load_diffusion_model_state_dict`
- llama.cpp 格式（融合 qkv、`ssm_a`/`attn_gate`）或 `detect_te_model` 认不出的 GGUF，加载前直接拦住并给中文说明（否则会悄悄变成 768 维 SDXL CLIP，编码结果是错的）
## v1.7.66 变更

- 「远程 GGUF 生成」删掉「服务端地址 / GGUF 模型 / 视觉模型」三行输入；地址/模型/mmproj 一律取「远程 GGUF 加载器」句柄，不接 gguf 直接报中文错误
- 新增「输出提示词语言」下拉：跟随用户输入 / 英文 / 中文（默认跟随，按用户提示词是否含汉字判定），以追加系统提示词的方式约束正/负向提示词语言
## v1.7.65 修复

- 「卸载到内存」对 GGUF 不生效（显存不降）：`llm_worker.cmd_load` 里 `n_gpu_layers=0` 被 `or -1` 吃成「全上卡」，且同模型短路没比较层数 → 已修（0 原样保留、层数变了强制重载）
- `llm_offload_ram` 会回读 worker 的层数，不等于 0 时在日志/返回值里提示「服务端 exe 可能是旧版」
## v1.7.64 变更

- 新增 **远程 GGUF 模型加载器 (Remote GGUF → 扩散模型)**：把服务端 `.gguf` 用 `comfy.sd.load_diffusion_model` 当 DiT/UNet 加载，输出 `模型`（`REMOTE_MODEL`）
- 新增 **远程 K 采样器 (Remote KSampler)**：`模型 + 两路条件 + latent` 发服务端，`comfy.sample.sample()` 采完把 latent 回传；输出 `latent` → 接「远程 VAE 解码」
- 服务端新增 `unet_load_gguf` / `unet_sample` 两个 op，`_loaded` 增加 `unet` 类（`max_loaded.unet` 默认 1）
- 原生 KSampler 无法接这个句柄：权重在服务端，本机没有模型对象，采样必须一起搬过去

## v1.7.63 变更

- **「卸载模型」新增档位「卸载到内存」**，并成为所有节点的默认：运行后把权重从显存搬到系统内存（缓存不丢），下次用只做内存→显存搬运，不再重读磁盘；加载器节点在该档下不会刚加载完就搬走
- 服务端 `unload` 新增 `mode=ram`；新增 `op=llm_offload_ram`（GGUF 以 0 层上卡重载 = 权重进内存、显存释放）
- 修复：`RemoteCLIPTextEncode` 未接 clip 时 `proxy` 未初始化导致的 `UnboundLocalError`
# comfyui_remote_clip_vae（ComfyUI 侧插件）

装到出图那台电脑的 `<ComfyUI>/custom_nodes/` 下。把它整体拷过去，重启 ComfyUI 即可。
通常直接运行上一层的 `3_安装插件到ComfyUI.bat` 更省事（会自动写入服务器地址）。

节点分类：**`comfyui_remote_clip_vae`**

## 节点

| 节点 | 输出 | 说明 |
|---|---|---|
| 远程 CLIP 加载器 (Remote CLIP Loader) | `clip` | 句柄：服务端地址 + 模型名，本机不占显存 |
| 远程 VAE 加载器 (Remote VAE) | `vae` | 同上 |
| 远程 CLIP 文本编码 (Remote CLIP Encode) | `正向条件` / `负向条件` | 一个节点出两路 CONDITIONING，直接接 KSampler；**只有文本输入，不接图片** |
| 远程 CLIP 图文编码 (Remote CLIP Encode + Images) | `正向条件` / `负向条件` | 文本 + 参考图（`图片1…图片10` 接一张自动多一个空槽）；接了 `vae` 就同时编 `reference_latents` |
| 远程 VAE 解码 (Remote VAE Decode) | `图像` | 服务端解码，回传 PNG |
| 远程 VAE 编码 (Remote VAE Encode) | `latent` | img2img / 重绘 |
| 远程 GGUF 加载器 (Remote GGUF Loader) | `gguf` / `状态` | v1.7.61 新增：把服务端 GGUF（含 mmproj）装进服务端显存，输出句柄给下面的生成节点复用 |
| 远程 GGUF 生成 (Remote GGUF Generate) | `正向条件` / `负向条件` / `提示词` / `负向提示词` | v1.7.61 新增：用服务端 GGUF 生成提示词；接上 `clip` 时直接出 CONDITIONING（接 KSampler，与 UNet 加载器同链路），不接只出文本给「远程 CLIP 文本编码」 |
| 远程 GGUF 文本编码器 (Remote GGUF → CLIP) | `clip` / `状态` | v1.7.62 新增：把服务端 LLM 目录里的 `.gguf` 当文本编码器加载，输出 **CLIP**（可直接接原生 `CLIPTextEncode` /「文本编码 Qwen Image 编辑 Plus」）；编码在服务端 `sd.load_clip` 跑，**服务端 ComfyUI 需装 ComfyUI-GGUF**；只支持文本，带图请用「远程 CLIP 图文编码」 |

两个「加载类」节点字段顺序一致：**服务端显卡 / 计算设备 / 服务端地址 / 模型**
（CLIP 加载器多一个「模型类型」），接着是「卸载模型」和整宽按钮 **「🔄 刷新模型列表」**。

- **服务端显卡**：只读，点刷新自动填服务端**显卡型号**（只有型号，不带 CUDA/ROCm 这类计算方式，也不带显存）；
  服务端是纯 CPU 时显示「（无显卡，纯 CPU 计算）」，连不上显示「（连不上服务端）」
- **计算设备**：只读，点刷新自动识别远端到底在用**显卡**还是 **CPU** 算（值就是「显卡」/「CPU」）；
  以前这个位置是让你手选 `auto / cpu / gpu`，v1.7.32 起取消手选，完全自动
- **模型**：服务端 `config.json` 里的条目名；连不上时是「（连接失败，检查服务端）」，ComfyUI 照常能启动
- **模型类型**：`自动（按文件名判断）` = 用服务端 `clip_type`；也可手动指定 `flux` / `sd3` / `qwen_image` …（仅 CLIP 加载器）
- **v1.7.32 起所有节点的「手动模型名」已彻底删掉**（加载器 v1.7.24 起就没有，文本编码 / 图文编码也在这一版删掉）：
  模型只从「🔄 刷新模型列表」拉下来的下拉里选；老工作流里残留的那个值一律忽略，不会报错

### 文本编码节点（v1.7.12 起只有文本）

- `正向提示词` / `负向提示词` 各一个多行框，输出 `正向条件` / `负向条件` 两路，直接进 KSampler / 采样器
- 可选输入只有 `clip`（接「远程 CLIP 加载器」）；原来的 `device`（auto/cpu/gpu）已删除（v1.7.32），
  条件张量一律跟原生节点一致放在本机模型设备上，不用再选
- **v1.7.43：`clip` 接上后，本节点的「服务端显卡 / 计算设备 / 服务端地址 / 模型 / 模型类型」+
  「🔄 刷新模型列表」自动收起，模型名与地址一律取上游加载器那一份**；下拉里即使是
  `（连接失败，检查服务端）` 也不会再弹「2 无效输入」（`VALIDATE_INPUTS` 认 `input_types`，
  `clip` 连着就直接放行）。断开连线立刻恢复原样。
- **v1.7.7~v1.7.11 那套 `图片模式` + `图片1…图片32` + `vae` / `视觉模型` / `参考图像素` 已整套移除**，
  节点上不会再出现图片接口，前端也不会自动增槽。需要图文混编请改用 ComfyUI 原生
  `TextEncodeQwenImage21`（配 `qwen3vl_8b_bf16`），本包的加载器照样能给它供 `clip` / `vae`。

### 图文编码节点「远程 CLIP 图文编码」（v1.7.14 起，v1.7.15 上限 10 张）

- 显示名 `远程 CLIP 图文编码 (Remote CLIP Encode + Images)`，字段与文本编码一致，可选输入多一个 `vae`
- `参考图最大像素`（v1.7.35 起）：**单位百万像素（MP）**，默认 1.00（≈1000×1000），只缩不放、宽高对齐 32；
  `0` = 保持原尺寸。老工作流里那个「参考图像素」(1048576 这种) 仍然认，会直接按像素数走
- `图片1…图片10`：**接一张自动长一个槽**（前端 `refresh.js` 动态加，最多 10）
- 接了 `vae` → 参考图同时进视觉槽并编 `reference_latents`（`keep_vision=False`，等同原版 2.0 编辑 Plus）
- 不接 `vae` → 只走视觉槽（`keep_vision=True`）

### 提示词优化节点「远程提示词优化」（v1.7.16 起，v1.7.17 改走服务端 GGUF）

- 输入（v1.7.33 现状）：`用户提示词` / `是否反推` / `使用远程模型` / `服务端地址` / `API Key` / `文生图模型` / `图生图模型` /
  `视觉模型` / `参考图最大像素`(MP) / `输出语言` / `透明背景` / `种子值`（右侧带「固定值/递增值/递减值/随机值」）/
  `卸载模型` + `图片1…图片10`
- **v1.7.33：「种子值」和「种子模式」合成一个控件**：就是 ComfyUI 原生的 seed + control 样式
  （`control_after_generate`）——数值框右边一个按钮，点开是 固定值 / 递增值 / 递减值 / 随机值，默认「随机值」
- 输出：`提示词` / `负向提示词` / `是否反推`（v1.7.40 起反推由节点上的「是否反推」开关决定：开=按系统+用户提示词反推图，关=用系统优化提示词）
- `接口类型` 三选一（默认第一项）：`RemoteCLIPVAE 服务端 (用远端 ComfyUI 里的 GGUF)` / OpenAI 兼容 / Ollama 原生
- 选第一项时，模型在**服务端机器的 `<ComfyUI>/models/LLM`**，由服务端那份 ComfyUI 的 python 加载；
  服务端面板能看「LLM 运行实时信息」、能一键装 llama-cpp-python

## 文件

| 文件 | 作用 |
|---|---|
| `nodes.py` | 7 个节点（CLIP 加载 / VAE 加载 / 文本编码 / 图文编码 / 提示词优化 / VAE 解码 / VAE 编码） |
| `api.py` | 本机代理路由 `/remote_clipvae/probe`（浏览器跨域会被挡，刷新按钮走它） |
| `web/refresh.js` | 「🔄 刷新模型列表」按钮、显卡栏只读、改动地址自动重探（无图片槽逻辑） |
| `codec.py` | 张量与图像的序列化协议，**必须与 `model_server/codec.py` 完全一致** |
| `remote_api.py` | 访问服务端的 HTTP 客户端，只用标准库 |
| `settings.py` + `default_server.json` | 默认服务器地址（`3_` 脚本会写入） |

## 默认服务器地址

`default_server.json`：

```json
{ "url": "http://192.168.1.20:8199", "token": "" }
```

也可用环境变量覆盖：`REMOTE_CLIPVAE_SERVER`、`REMOTE_CLIPVAE_TOKEN`。

## 注意

- `REMOTE_CLIP` / `REMOTE_VAE` 只是字典句柄（地址 + 模型名），**不占显存**
- 下拉框的模型名默认来自服务端 `config.json`；服务端重扫后点「🔄 刷新模型列表」即刻生效，不用重启 ComfyUI
- 服务端没启动时下拉框显示「（连接失败，检查服务端）」，ComfyUI 仍能正常启动
- 修改 `codec.py` 后必须同步 `model_server/codec.py`，否则协议对不上
- 如果浏览器缓存了旧版 `refresh.js`，按 `Ctrl+F5` 强刷一次 ComfyUI 页面
