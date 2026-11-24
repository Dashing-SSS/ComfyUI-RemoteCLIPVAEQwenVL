## 本次更新 v1.7.71 —— 删掉「远程 GGUF 模型加载器」「远程 K 采样器」两个节点

按你的要求，这两个节点从节点列表里彻底移除（不是隐藏）：

- `RemoteGGUFUNETLoader`「远程 GGUF 模型加载器 (Remote GGUF → 扩散模型)」
- `RemoteKSampler`「远程 K 采样器 (Remote KSampler)」

删掉的内容包括节点类本体、`NODE_CLASS_MAPPINGS` / `NODE_DISPLAY_NAME_MAPPINGS` 里的登记、
前端刷新映射（`web/refresh.js` 里那条 `unet_gguf` 规则）、以及配套已无人使用的 `REMOTE_MODEL_TYPE` 常量。
插件现在对外只剩 11 个节点：

```
远程 CLIP 加载器 / 远程 VAE 加载器 / 远程 CLIP 文本编码 / 远程 CLIP 图文编码 / 提示词优化
远程 VAE 解码 / 远程 VAE 编码 / VAE 自检 / 远程 GGUF 加载器 / 远程 GGUF 生成 / 远程 GGUF 文本编码器
```

服务端 exe 不用换（后端 `unet_load_gguf` / `unet_sample` 接口保留着，只是前端不再有节点调用它）；
包里的 exe 仍是 v1.7.70。老工作流里如果还摆着这两个节点，会显示成缺失节点，直接删掉即可。

本机实测：

```
[remote-clipvae] 节点已加载：…（11 个）
节点数: 11
两个节点已不存在
```

---
## 本次更新 v1.7.70 —— 扩散模型 GGUF 报「Unknown model architecture」时把文件的底细说出来

你贴的这条报错：

```
unet_load_gguf 失败（HTTP 400）：这个 GGUF 不能当扩散模型加载：
This model is not currently supported - (Unknown model architecture!)
```

含义是：文件读得出来（gguf 解包成功），但 ComfyUI 的 `detect_unet_config` 认不出它的结构 ——
既不是它认识的 DiT，也不是我们上一版拦掉的 LLM。只凭这一句没法判断是「新架构的 DiT」还是
「文本编码器 gguf」，所以这一版把判断依据直接打进错误信息：

- 读 GGUF 头部的 `general.architecture` / `general.name` / 张量数，加载前先打到服务端日志：
  `[服务端] GGUF 头信息：arch=qwen_image name=None tensors=1933`
- 认不出结构时，错误信息里给出：arch、张量数、**键名分布**（如 `blk.(401)、token_embd(1)`），
  并按 arch 给出结论：属于文本编码器/大模型，还是 ComfyUI 还不支持的 DiT 架构
- 顺手加了前缀自愈：键名多带一层 `model.` / `transformer.` / `net.` / `diffusion_model.` 前缀时，
  逐个剥掉重试，能认出来的就直接加载成功（日志会写「键名带前缀…已剥掉后加载成功」）
- 拦截名单补了 llama.cpp 的键名（`blk.*` / `token_embd` / `output_norm`），
  这类文本编码器 gguf 不会再走到 ComfyUI 报 Unknown 才失败

本机实测：

```
GGUF 头信息：arch=qwen_image name=None tensors=1933     ← 真 DiT
-> ModelPatcher 2.6s
GGUF 头信息：arch=qwen35  name=Qwen Image 2.1 PE I2I  tensors=427
拦住：… Unexpected architecture type in GGUF file: 'qwen35' / 这是 LLM 格式，arch=qwen35…
```

所以：装 v1.7.70 再点一次，服务端日志里会出现那行 `GGUF 头信息：arch=…`，
把那行（以及新的报错全文）贴回来，就能确定 `qwen-image-2.1-Q8_0.gguf` 到底是什么东西 ——
是 Qwen-Image 2.1 的真 DiT（那就要给 ComfyUI 补 2.1 的识别），还是别的工具的文本编码器。

---
# RemoteCLIPVAE —— 局域网双机 ComfyUI 分布式（CLIP 文本编码 + VAE 编解码放另一台机）

```
出图电脑：ComfyUI 主程序                      服务端电脑：本包的服务端
┌──────────────────────────────┐   HTTP    ┌────────────────────────────────┐
│ Load Diffusion Model (UNet)  │   JSON    │  CLIP 文本编码器（T5 / CLIP-L/G）│
│ KSampler ────────────────────┼─ prompt ─▶│  VAE（编码 + 解码）              │
│ 远程 CLIP 文本编码 ◀──────────┼─ COND ────│  直接调用 ComfyUI 的 comfy.sd    │
│ 远程 VAE 解码 ◀───────────────┼─ PNG ─────│  惰性加载 + LRU + 空闲释放        │
└──────────────────────────────┘           └────────────────────────────────┘
```

- 出图电脑的显存**全部留给图片生成模型**，不再放 CLIP 和 VAE
- 文本编码结果是纯张量，跨机传输**无损**（float32 原样，不压 8bit）
- 图像走 PNG 压缩（1024×1024 约 1~2 MB），千兆局域网单张往返 20~40 ms
- 服务端**不另写一套 CLIP 实现**，而是调用 服务端电脑 ComfyUI 的 `comfy.sd`，所以和 本机 UNet 完全同源
---

## 本次更新 v1.7.69 —— 「远程 GGUF 模型加载器」开始扫扩散模型目录（`qwen-image-2.1-Q8_0.gguf` 这类能用）

上一版之后你问「`qwen-image-2.1-Q8_0.gguf` 我有这个，不像吗」—— 像，那个才是真扩散模型 GGUF。
但当时节点**只看服务端 LLM 目录**，放在 `models/diffusion_models` / `models/unet` 里的 gguf
既不出现在下拉里，选了也报「找不到这个 GGUF」。

改动：
- 新增模型类别 `unet_gguf`：递归扫 `models/diffusion_models`、`models/unet`、`models/GGUF`
  （包括面板「模型根目录」里的同名子目录），深度 12 层，显示名是相对路径（如 `GGUF/qwen-image/xxx.gguf`）
- 「远程 GGUF 模型加载器」的「GGUF 模型」下拉改用这个清单；老服务端没有这个类别时自动退回 llm 目录
- 加载时名字支持三种写法：相对路径、纯文件名、绝对路径；缓存键改成**绝对路径**，
  同一个文件不管怎么选都只加载一份
- 客户端「🔄 刷新模型列表」跟着改成 `unet_gguf`；`/remote_clipvae/probe` 的白名单也加了它，
  空清单时提示「把 .gguf 放到服务端 models/diffusion_models 或 models/unet 下」

本机实测（拿本机真实的 `qwen-image-2512-Q3_K_S.gguf`，8.59GB）：

```
发现 GGUF 扩散模型: 8
  GGUF/qwen-image/qwen-image-2512-Q3_K_S.gguf -> ...\diffusion_models\GGUF\qwen-image\...
[服务端] GGUF 扩散模型加载完成，耗时 0.3s
[服务端] GGUF 扩散模型「…」权重约 8.6GB，显存 11.9GB（空闲 9.7GB），可以整份放进去
handle 返回: {'loaded': 'GGUF/qwen-image/qwen-image-2512-Q3_K_S.gguf', 'device': 'cpu'}
缓存键: ['gguf::d:\...\qwen-image-2512-q3_k_s.gguf']   ← 按 basename 再取一次，命中缓存不重载
```

---

## 本次更新 v1.7.68 —— 扩散模型那条误用了文本模型的读取器

日志里 `Unexpected text model architecture type in GGUF file: 'qwen35'` 是**我上一版的 bug**：
`_load_diffusion_gguf` 挑函数时先挑到 `gguf_clip_loader`（`is_text_model=True`），
于是把扩散模型的 gguf 当成"文本模型"去校验 arch，直接报这句；随后两条 fallback 又走回 `torch.load`，
才有了后面那两串 `Weights only load failed`。

修法：
- 加载扩散模型一律用 `gguf_sd_loader`（`handle_prefix="model.diffusion_model."`）
- 文本编码器用 `gguf_clip_loader`（或 `gguf_sd_loader(is_text_model=True)`），两个函数分开存
- 扩散模型再加一道结构判定：只有 `model.layers.*`、`q_projkv`、`embed_tokens` 这类 LLM 层、
  没有 `double_blocks / input_blocks / transformer_blocks` 的，直接判定「这是 LLM 格式，不是扩散模型」
- `get_unet_gguf` 不再把上面的判定结果吞进 fallback 里，原样抛给客户端

本地实测（同款 5.87GB `Qwen-Image-2.1-PE-I2I.Q5_K_S.gguf`）：

```
扩散模型这条：这个 GGUF 不能当扩散模型加载：Unexpected architecture type in GGUF file: 'qwen35'
            这是 LLM / 文本模型格式的 GGUF（arch=qwen35），不是扩散模型权重，不能当 UNet/DiT 加载。
文本编码器这条：这个 GGUF 是 llama.cpp / 大模型（LLM）格式：张量是融合过的（例如 model.layers.0.attn_gate.weight）…
```

**结论（重要）**：`Qwen-Image-2.1-PE-*` 是提示词增强模型，架构 `qwen35`，
既不能当 CLIP、也不能当 UNet —— 它只该出现在「远程 GGUF 加载器 → 远程 GGUF 生成」这条链路上。
服务端采样要真跑，得给服务端一个**真正的扩散模型权重**（safetensors 的 UNet/DiT，或 ComfyUI 格式的 DiT gguf）。

---

## 本次更新 v1.7.67 —— 修「装了 ComfyUI-GGUF 还是 weights_only 报错」，并查清 PE 那个 GGUF 根本不能当 CLIP

**为什么装了也没用**：服务端 exe 只 `import comfy.sd`，**从来没有加载过 custom_nodes** ——
`comfy.sd.load_clip` 里走的是 `comfy.utils.load_torch_file`（`torch.load`），读 `.gguf` 直接
`Weights only load failed`。装多少个 GGUF 插件都不会被这行代码看到。

修法：不再指望 `load_clip`，自己去 `custom_nodes` 里找带 `ops.py`（或 `ops/`）+ `loader.py` 的 GGUF 包
（`ComfyUI-GGUF`、`ComfyUI-GGUF-VLM` 这类都行），把它的 `GGMLOps` 和 `gguf_clip_loader` import 进来，
然后走 ComfyUI 的正确入口：
`comfy.sd.load_text_encoder_state_dicts(state_dicts=[sd], model_options={"custom_operations": GGMLOps})`。
扩散模型同理改成 `load_diffusion_model_state_dict(sd, model_options={"custom_operations": ...})`。

**更要紧的发现**：`Qwen-Image-2.1-PE-*.gguf` 这类文件是 **llama.cpp 的 LLM 格式**，张量是融合过的
（`self_attn.q_projkv.weight`、`ssm_a`、`attn_gate`）—— ComfyUI 的文本编码器认不出来，
真装下去会**悄悄退化成 768 维的 SDXL CLIP**（不报错，但编码结果全是垃圾）。
所以加载前加了结构判定，两种情况直接拦住并给中文说明：

- 有融合权重（`q_projkv` / `ssm_a` / `attn_gate`）→ 这是 LLM 格式，只能当 LLM 用
- `detect_te_model(sd)` 返回 None → 装不成 CLIP，再装只会得到错维度的模型

**这个东西该怎么用**：PE 就是提示词增强模型，走 `远程 GGUF 加载器 → 远程 GGUF 生成` 出提示词，
再交给「远程 CLIP 文本编码」（safetensors 的文本编码器）或 UNet 采样链路。

实测（本机同版本 ComfyUI 0.46.2 + 同款 5.87GB GGUF）：

```
[服务端] 找到 GGUF 支持：comfyui-gguf-loader（gguf_clip_loader + GGMLOps）
detect_te_model = None      ← 这就是它会退化成 SDXL CLIP 的原因
拦住： 这个 GGUF 是 llama.cpp / 大模型（LLM）格式：张量是融合过的（例如 model.layers.0.attn_gate.weight）…
```

---

## 本次更新 v1.7.66 —— 「远程 GGUF 生成」瘦身 + 新增「输出提示词语言」

节点上那三行 「服务端地址 / GGUF 模型 / 视觉模型」删掉了。地址、模型、mmproj 全部由**接进来的「远程 GGUF 加载器」句柄**决定（本来接了句柄就忽略它们，属于重复填写，还容易填得不一致）。
节点不再自带"自己 auto_load 一次"的兜底 —— 不接 gguf 时直接抛中文提示：把加载器的 gguf 连过来。

新增下拉 **输出提示词语言**：`跟随用户输入 / 英文 / 中文`（默认跟随）。
- 英文 → 系统提示词里追加"prompt 与 negative_prompt 必须全部用英文书写"
- 中文 → 追加"必须全部用简体中文书写，不要输出英文提示词"
- 跟随 → 用用户提示词里有没有汉字来判定（没写词时按英文，图像模型更稳）

实测（拦截 rpc 抓 system 原文）：

```
中文输入 + 英文档   → system 含「全部用英文」 True
中文输入 + 跟随     → system 含「全部用简体中文」 True
英文输入 + 跟随     → system 含「全部用英文」 True
不接 gguf           → ValueError: 这个节点要接「远程 GGUF 加载器」的 gguf 输出…
```

---

## 本次更新 v1.7.65 —— 修「GGUF 选卸载到内存后显存一点没降」

**现象**：节点选「卸载到内存」，服务端日志打出
`[服务端] GGUF …Q5_K_S.gguf 卸载到内存（0 层上卡）→ 显存 7.79GB / 11.99GB → 7.79GB / 11.99GB`
—— 显存前后一模一样，模型还在卡上。

**两个真凶，都在 `llm_worker.py` 的 `cmd_load()` 里**（v1.7.63 加的「0 层上卡」正好踩这俩坑）：

1. `n_gpu_layers = int(p.get("n_gpu_layers", -1) or -1)` —— 传进来的 **0** 被 `or` 吃成 **-1**，
   也就是「全部层上卡」。所以"卸载到内存"实际执行的是"再上一遍全卡"。
   改成：`_raw = p.get("n_gpu_layers", -1)`，`None/""` 才回落 -1，`0` 原样保留。
2. **同模型缓存短路**：只要 `模型路径 + mmproj` 相同就直接 `return cmd_info()`，
   **根本没看层数** —— 于是 0 层那次请求被当成"已经加载了，什么都不做"。
   改成短路条件再加上 `STATE["n_gpu_layers"] == n_gpu_layers`；层数变了就老老实实 `cmd_unload()` 再重载。

**顺带加的护栏**：`llm_offload_ram` 现在会回读 worker 的 `n_gpu_layers`，如果不是 0 就把
「0 层上卡没生效（服务端报告的层数是 X）——多半是服务端 exe 还是旧版」写进日志和返回值，
下次一眼能判断是代码问题还是没换 exe。

**验证（用假 Llama 打进 worker，直接验参数流）**

```
传 0 → n_gpu_layers=0 | STATE.n_gpu_layers = 0
同一模型再传 0（应命中缓存）→ 0（没有重新加载）
同模型传 -1（层数变了）→ 实际重新加载，n_gpu_layers = -1 | Llama 构造次数 2
```

---

## 本次更新 v1.7.64 —— GGUF 当「扩散模型」用：新增「远程 GGUF 模型加载器」+「远程 K 采样器」

你说得对，之前的 GGUF 加载器是 llama.cpp 的 LLM，输出的是 `REMOTE_GGUF` 句柄，物理上不可能接 KSampler 的 `model` 输入。
这一版把方案 2 做出来了：**GGUF 当 DiT/UNet 加载在服务端，采样也在服务端跑，本机只发 latent 收 latent。**

**新节点 1：远程 GGUF 模型加载器 (Remote GGUF → 扩散模型)**

- 输入：服务端显卡(只读) / 计算设备(只读) / 服务端地址 / **GGUF 模型** / 卸载模型
- 输出：**`模型`（类型 `REMOTE_MODEL`）+ `状态`**
- 服务端用 `comfy.sd.load_diffusion_model(unet_path=...)` 真加载（等价 ComfyUI-GGUF 的 `UnetLoaderGGUF`），
  缓存在 `self._loaded["unet"]`、键 `gguf::<名>`，同样支持「卸载到内存」

**新节点 2：远程 K 采样器 (Remote KSampler)**

- 输入：`模型`(REMOTE_MODEL) / `正向条件` / `负向条件` / `latent` / 种子值 / 步数 / CFG / 采样器 / 调度器 / 降噪 / 卸载模型
- 输出：**`latent` + `采样信息`**
- 采样器、调度器清单直接取 `comfy.samplers.KSampler` 的，名字和原生 KSampler 完全一致
- 只把 latent（几十 KB~几 MB）和条件发过去，服务端 `comfy.sample.sample()` 跑完把 latent 回传，
  再接「远程 VAE 解码」出图 —— 整条出图链路可以完全不占本机显存

**为什么必须用「远程 K 采样器」而不是原生 KSampler**

KSampler 的每一步都要在本地拿模型权重做前向。权重在服务端那台机器上，本机没有模型对象，
所以原生 KSampler 拿到这个句柄只会报类型不匹配。要“接 KSampler 的模型输入”这条路，
物理上只能把采样一起搬过去 —— 就是这两个节点干的事。

**服务端新增协议**

- `POST /rpc` `op=unet_load_gguf {model}` → `{loaded, device, loaded_kinds}`
- `POST /rpc` `op=unet_sample {model, latent, positive, negative, steps, cfg, sampler_name, scheduler, seed, denoise}` → `{samples, seconds}`
- `_loaded` 多一类 `unet`（`max_loaded.unet` 默认 1）；`unload(kind="unet")` 同样支持 `full` / `ram`

**接线（三选一都行）**

```
远程 GGUF 模型加载器 → 远程 K 采样器（模型）
远程 CLIP 文本编码  → 远程 K 采样器（正向/负向）
空 Latent / 远程 VAE 编码 → 远程 K 采样器（latent）
远程 K 采样器.latent → 远程 VAE 解码 → 出图
```

**实测（`app.py --mock` 真 HTTP + 插件真 import）**

```
节点数 13 | 新增: RemoteGGUFUNETLoader / RemoteKSampler
加载器输入: 服务端显卡/计算设备/服务端地址/GGUF 模型/卸载模型 | 输出 ('REMOTE_MODEL','STRING') ('模型','状态')
采样器输入: 模型/正向条件/负向条件/latent/种子值/步数/CFG/采样器/调度器/降噪/卸载模型
[远程GGUF-UNET] 句柄就绪：http://127.0.0.1:8296 | mock_llm.gguf | 服务端扩散模型（GGUF）
[远程K采样] mock_llm.gguf | euler/normal | 8 步 | CFG 4 | 降噪 1 | 0.1s | latent (1, 4, 64, 64)
采样输出: (1, 4, 64, 64)
```

> 服务端前置条件：ComfyUI 必须装 **ComfyUI-GGUF（city96）**（它提供 GGUF 的 ops）；
> 另外这支 `.gguf` 必须真的是扩散模型权重 —— 拿 LLM 的 gguf 当 UNet 加载会直接报结构不匹配。

---

## 本次更新 v1.7.63 —— 「卸载模型」新增「卸载到内存」档，并把它做成所有节点的默认

**以前只有两档真卸载**（运行后 / 运行前后）：卸完对象就没了，下次用必须重新读盘（CLIP 几十秒、GGUF 更久）。
现在多了「卸载到内存」：

- **语义**：运行结束后把权重从 **显存** 搬到 **系统内存**（CPU），显存照样还给你，但对象留在缓存里 ——
  下次这个节点再跑，只做一次「内存 → 显存」的搬运（PCIe 直传，几 GB 也就零点几秒），**不用重读磁盘、不用重建 tokenizer**
- **默认**：所有带「卸载模型」的节点（CLIP 加载器 / VAE 加载器 / CLIP 文本编码 / CLIP 图文编码 / 提示词优化 /
  VAE 解码 / VAE 编码 / GGUF 加载器 / GGUF 生成 / GGUF 文本编码器）默认都改成「卸载到内存」
- **加载器节点特殊处理**：加载器（CLIP/VAE/GGUF/GGUF-CLIP）的职责就是把模型交给下游，
  所以它在「卸载到内存」下**不会**刚加载完就搬走（否则下游立刻又要搬回来/重读一遍盘）；真正搬的是执行类节点
- **GGUF 侧**：llama.cpp 没有显存↔内存分页，所以走的是「同一支 GGUF 以 0 层上卡重载」——
  权重落在内存、显存释放，下次 `llm_load/llm_chat` 按节点自己的「上卡层数」搬回显存（读盘命中系统缓存，很快）

**服务端协议变化**

- `POST /rpc` `op=unload` 多了 `mode` 参数：`mode="ram"` → 只搬内存不丢缓存；不传/`full` → 老行为（真卸载）
- 新增 `POST /rpc` `op=llm_offload_ram` → 把当前 GGUF 以 `n_gpu_layers=0` 重载，返回 `{moved, note, vram_before, vram_after}`
- 搬内存的实现：对 `cond_stage_model / first_stage_model / patcher.model` 这些真权重调 `.to("cpu")`，
  记下原设备；`get_clip/get_vae/get_clip_gguf` 命中缓存时先自动搬回显存，搬不回去就丢掉缓存走正常重载
- 服务端日志会区分两种措辞：`卸载到内存 … → 搬到内存=[...] 显存 X → Y` / `卸载 … → freed=[...] 显存 X → Y`

**实测（`app.py --mock` 真 HTTP + 插件真 import）**

```
UNLOAD_MODES: ['卸载到内存','自动','保持加载','运行后自动卸载','运行前后自动卸载'] | 默认: 卸载到内存
10 个带「卸载模型」的节点：选项=5 默认=卸载到内存（逐个校验 OK）
plan 卸载到内存: (False, True) | 加载器节点: (False, False) | 运行前后: (True, True)
mode: 卸载到内存 → ram ；自动 → full
ram unload → {"freed": [], "mode": "ram", "vram_before": ...}
llm_offload_ram → {"moved": [], "note": "服务端当前没有已加载的 GGUF，跳过"}
[远程卸载] 文本编码(运行后): clip=mock_clip_768 → 搬到内存 … | 文本编码 默认模式: (1, 77, 768)
```

> 顺带修掉一个 v1.7.62 遗留的崩溃：`RemoteCLIPTextEncode.encode` 在「没接 clip」时 `proxy` 未初始化 →
> `UnboundLocalError: cannot access local variable 'proxy'`。现在开头就把 `proxy/handle` 置 None。

---

## 上一版 v1.7.62 —— 补「刷新模型列表」按钮 + 新增「远程 GGUF 文本编码器（输出 clip）」节点

**1）三个 GGUF 节点补上「🔄 刷新模型列表」**

v1.7.61 那三个节点只有下拉、没有刷新按钮（`web/refresh.js` 里没登记它们的节点名，所以按钮没被插进去）。现在 `KIND_BY_NODE` 已登记：

- `RemoteGGUFLoader` / `RemoteGGUFGenerate` / `RemoteGGUFCLIPLoader`：刷新「GGUF 模型」（llm）+「视觉模型」（llm_vision），同时把「服务端显卡 / 计算设备」两栏填上
- 点一下刷新就会重新拉 `/health.models` 的 llm / llm_vision 清单；改「服务端地址」后也会静默重拉一次（900ms 防抖）

**2）新增节点：远程 GGUF 文本编码器（Remote GGUF → CLIP）**

这就是你要的「加载远程 GGUF 输出 clip 模型」：

- 输入：服务端显卡(只读) / 计算设备(只读) / 服务端地址 / **GGUF 模型** / **模型类型**(默认 `qwen_image`) / 卸载模型
- 输出：**`clip`（类型就是 CLIP）+ `状态`**
- 内部：`RemoteGGUFClip` 代理，本机零加载；`tokenize` / `encode_from_tokens_scheduled` 全打到服务端新 op `gguf_encode`，
  服务端用 `comfy.sd.load_clip(ckpt_paths=[那支 .gguf])` 真加载（走 ComfyUI-GGUF 的 ops），再编码成 CONDITIONING
- 可接的地方：原生 `CLIPTextEncode`、「文本编码 Qwen Image 编辑 Plus」、本插件的「远程 CLIP 文本编码 / 图文编码」、
  「远程 GGUF 生成」的 `clip` 口（后者接上就直接出 `正向条件/负向条件`，跟 UNet 那条 KSampler 链路一起用）
- 限制：**只有文本**。GGUF 的 mmproj 视觉塔没法按 `sd.load_clip` 装进 CLIP，所以带图会被明确拦住并提示改用「远程 CLIP 图文编码」
- 服务端前置条件：服务端 ComfyUI 必须装 **ComfyUI-GGUF（city96）**。没装时加载会直接报一句人话，不会给你一个空 clip

**服务端新增协议**（已打进新 exe）

- `POST /rpc` `op=gguf_encode` → `{model, text, clip_type, last_layer, images}` → `{conditioning, count, gguf}`
- CLIP 缓存新增 `gguf::<模型名>` 这一族；`unload(kind=clip, model=X)` 会同时匹配 `X` 与 `gguf::X`

**实测（`app.py --mock` 真 HTTP + 插件真 import）**

```
节点数 11 | GGUF 系列: RemoteGGUFLoader / RemoteGGUFGenerate / RemoteGGUFCLIPLoader
输入: 服务端显卡/计算设备/服务端地址/GGUF 模型/模型类型/卸载模型
输出: ('CLIP','STRING') ('clip','状态')
[远程GGUF-CLIP] 句柄就绪：http://127.0.0.1:8296 | mock_llm.gguf | clip_type=qwen_image | 服务端文本编码器（GGUF）
clip 类型: RemoteGGUFClip | tokenize/encode 齐备
直接编码: 1 条条件, shape (1, 77, 768)
[远程CLIP文本编码] 用上游「远程 GGUF 文本编码器」编码：mock_llm.gguf → 正向 (1,77,768) / 负向 (1,77,768)
图文编码节点接 GGUF clip + 图 → 报错（符合预期）: 接的是「远程 GGUF 文本编码器」，它只有文本、没有视觉塔…
GGUF 生成 + 接 GGUF clip → 正向条件 (1,77,768) | 提示词: MOCK 远端提示词（0 张图）：黑猫
```

---

## 上一版 v1.7.61 —— 新增两个节点：远程 GGUF 加载器 + 远程 GGUF 生成（可接 CLIP / UNet）

以前只有「提示词优化」一个大节点，它自己 `auto_load` 服务端 GGUF，既看不到加载状态，也没法让多个下游共用同一份已加载的模型。这次把它拆成两半：

**1）远程 GGUF 加载器（`RemoteGGUFLoader`）**

- 输入：服务端显卡(只读)、计算设备(只读)、服务端地址、GGUF 模型、视觉模型(mmproj，可「不使用」)、上下文长度、上卡层数(-1=全部上卡)、卸载模型
- 输出：`gguf`（自定义类型 `REMOTE_GGUF` 句柄：url/token/model/mmproj/n_ctx/n_gpu_layers）+ `状态`（字符串，例如 `Qwen-Image-2.1-PE-I2I.Q5_K_S.gguf + mmproj-bf16.gguf | 已加载 | AMD Radeon RX 5700 XT | Qwen25VLChatHandler`）
- 实际动作：调服务端 `llm_load`，把 GGUF 真装进服务端显存；「卸载模型」四档照旧生效

**2）远程 GGUF 生成（`RemoteGGUFGenerate`）**

- 输入：`gguf`(可选，接加载器)、`clip`(可选)、用户提示词、是否反推、系统提示词(留空用内置)、最大输出长度、温度、种子值、参考图最大像素、卸载模型、图片1~4
- 输出：**正向条件 CONDITIONING / 负向条件 CONDITIONING / 提示词 STRING / 负向提示词 STRING**
- 两条连线都支持：
  - **接 CLIP（→ UNet 采样）**：`clip` 接「远程 CLIP 加载器」，本节点在服务端把 GGUF 生成的提示词再编码成条件，`正向条件`/`负向条件` 直接接 KSampler（正向/负向），KSampler 的 model 来自 UNet 加载器 —— 这条链路里 GGUF 只需要出文本，不占 UNet 显存
  - **只出文本**：不接 clip 时两个条件输出为空（None），把 `提示词`/`负向提示词` 接「远程 CLIP 文本编码」再出条件
  - **不接 gguf 也能跑**：本节点用自己的「服务端地址 / GGUF 模型 / 视觉模型」字段 `auto_load` 一次
- 维度判定和「提示词优化」一致：接了图就必须有 mmproj，否则明确报错；「是否反推」开着但没图也明确报错

**实测（`app.py --mock` 起真 HTTP 服务端，插件真 import）**

```
节点数: 10 | 新节点: ['RemoteGGUFLoader', 'RemoteGGUFGenerate']
加载器输入: 服务端显卡/计算设备/服务端地址/GGUF 模型/视觉模型/上下文长度/上卡层数/卸载模型
加载器输出: ('REMOTE_GGUF', 'STRING') ('gguf', '状态')
生成器输出: ('CONDITIONING','CONDITIONING','STRING','STRING') ('正向条件','负向条件','提示词','负向提示词')

[远程GGUF] 已加载：mock_llm.gguf + mock_mmproj.gguf | 已加载 | MOCK | MOCK
[远程GGUF生成] mock_llm.gguf | 图 0 张 | 写词 | 30 tok / 300 tok/s | 正向 26 字 / 负向 6 字
接 clip 后: 正向条件 list 长度 1, shape (1, 77, 768)；负向条件 长度 1
```

server 端不需要改代码，直接沿用现有 `llm_load` / `llm_chat` / `llm_unload`，所以**本次 exe 不用重打**（仍是 v1.7.60 那份）。

---

## 上一版 v1.7.60 —— 修掉面板 RPC 结果没解包（跑分 `PP None` / 模型名 `?` 的真凶）

**现象**：日志里明明 `LLM 操作完成：state=已加载`，紧接着跑分打印

```
[跑分] 模型 ?  后端 ?  设备 ?
  预填 PP: None tokens/s    生成: None tokens/s    （None tokens / Nones，数据来自 —）
```

——请求是成功的，返回就是读不出字段。

**真因（v1.7.59 修错地方了，这次是协议层）**：服务端 `/rpc` 返回前会用 `codec.pack(result)` 打标，
字典被包成 `{"__d__": {...}}`、列表包成 `{"__l__": [...]}`；插件侧 `remote_api.py` 收到后会 `unpack()`，
**面板 `_http_rpc` 从来没解过包**，直接 `r["data"].get("pp_tps")` → 全 None、`get("model")` → `?`。
所以不管 llama.cpp 有没有打计时行，面板都拿不到数字。

**改法**：`gui.py` 新增模块级 `_unpack(p)`（`import codec` → `codec.unpack`，失败就原样返回），
`_http_get` / `_http_rpc` / `_http_post_json` 三处返回值统一过一遍。GET 端本来就是明文，解包无副作用。

顺带修好了同一根因的其它地方（这些以前也都读不到字段，只是不报错）：
`llm_load` 的 `device_name` / `backend` / `handler` / `gpu_buf_mib`、`llm_env`、`llm_uninstall`、
`clear_vram` 的 `freed` / `vram_before` / `vram_after`、以及 `/reload` 的返回内容。

**实测（真 HTTP，不是 MockBackend 内存直调）**：`app.py --mock --port 8291` 起服务，面板侧 `_http_rpc("llm_bench")`：

```
llm_bench ok = True
  data 的键: ['backend', 'device_name', 'gen_tps', 'log', 'model', 'pp_tps', 'verdict', ...]
  pp_tps = 300.0  gen_tps = 300.0  model = mock_llm.gguf  backend = MOCK
  verdict = MOCK 跑分
llm_load data 键: ['backend', 'cpu_buf_mib', 'detail', 'device_name', 'dirs', ...]
```

（改之前同一条请求拿到的是 `{"__d__": {...}}` 外壳，所有字段读空 —— 这就是 `PP None` 的来源。）
配合 v1.7.59 的三种日志写法 + 自己计时兜底，现在无论是 llama.cpp 打不打 perf 行，面板都能出数。

---

## 上一版 v1.7.59 —— GPU 跑分能出数了（不再显示 PP None / 生成 None tok/s）

**现象**：点「GPU 跑分（预填/生成）」，面板上显示 `PP None / 生成 None tok/s`，日志里也没有实际速度。

**原因**：跑分完全依赖从 llama.cpp 的 `llama_perf_context_print` 日志里抠数字，而抠的那段只认一种写法：

```
prompt eval time =  381.66 ms /  600 tokens ( 0.64 ms per token, 1572.10 tokens per second)
```

必须同时有后半段 `tokens per second` 才认。可是新版 llama.cpp / Vulkan 构建在 `verbose=False` 时经常只打前半段
（`prompt eval time = 1200.00 ms / 512 tokens`），甚至一条计时行都不打 —— 于是 `pp` / `gen` 全是 0，
面板拿到 0 就当「没有」，直接显示成 `None`；日志里也被过滤成空。

**改法**

1. `llm_worker._perf_tps` / 新增 `_tps_from_line`：一行日志认三种写法 ——
   `N tokens per second`、`X ms / N tokens`（自己算 N/(X/1000)）、`X ms per token`（1000/X）。
2. **兜底实测**：日志里一条计时行都没有时，`cmd_bench` 用两次真实调用自己计时 ——
   一次只预填（`max_tokens=1`，得 PP = prompt tokens / 耗时），一次短提示生成 `gen_tokens` 个 token，
   再和「短提示只生成 1 个 token」那次相减，拿到纯生成速度。
   返回值多了 `tps_source`（`llama.cpp 计时行` / `自己计时（日志里没有 perf 行）`），面板日志里直接标出来。
3. 面板 `llm_bench`：拿到数字就显示 `PP 1520 / 生成 64 tok/s`；只有生成速度就显示 `PP 没测到 / 生成 38 tok/s`；
   一个都没有显示 `跑分失败：没拿到计速数据，看日志`，**再也不会出现 None**。
   日志行里补上「数据来自 llama.cpp 计时行 / 自己计时」，出问题时一眼能看出是哪种。
4. 日志过滤放宽：除了 `perf` / `offload`，也抓 `eval time`、`tokens per second`、`buffer size`、`graph` 这些行，便于排查。

**实测**

日志解析（三种写法 + 一条都没有）：

```
新: 只有 ms / tokens              -> (1572.1, 78.82)
只给 ms/N（无 tokens per second）  -> (426.67, 71.11)
只给 ms per token                -> (1250.0, 50.0)
啥都没有                          -> (0.0, 0.0)   ← 触发下面的兜底
```

兜底计时（假 llama：每次调用按睡眠时间模拟，日志里没有 perf 行）：

```
pp_tps: 2457.76   gen_tps: 125.05
tps_source: 自己计时（日志里没有 perf 行）
backend: vulkan   verdict: 显卡在算（生成速度正常）
```

面板文案：

```
(1520.3, 64.2)  -> PP 1520 / 生成 64 tok/s
(0.0, 38.0)     -> PP 没测到 / 生成 38 tok/s
(None, None)    -> 跑分失败：没拿到计速数据，看日志
```

> 提示：兜底计时的数字比日志口径略粗（含一点 Python 调用开销），但足以判断「到底谁在算」。
> 如果你的服务端本来就不打 perf 行，日志里会写「数据来自 自己计时」，这是正常的。

---

## 上一版 v1.7.58 —— 运行状态里的显存占用与实际对齐

**现象**：面板「运行状态」里的显存数字跟任务管理器 / AMD Adrenalin 对不上（有时偏小、有时干脆一直是同一个数），
利用率一栏常常是「—」；GPU 名字后面还老挂着 `[cuda]`，明明是 AMD。

**原因**

1. 显存只取了 `torch.cuda.mem_get_info()` 的 `total - free`。在 ROCm（AMD RX 5700 XT）上这个 free 跟
   系统里看到的占用量不是一回事：驱动/其他进程（`llama-server` 那种独立进程）占的显存算不进来，
   ROCm 有时还报不准，于是数字一直跟实际不同步。
2. 利用率只走 `nvidia-smi`（面板自己的 `gpu_stats()`），AMD 机根本没有 nvidia-smi → 永远「—」。
3. 标签写死 `[cuda]`；`torch_allocated_mb`（本进程占用）和服务端上报的 `vram` 也没一起展示。

**改法**

- `backend.py` 新增 `gpu_probe()`：按 **nvidia-smi → rocm-smi（`--showmeminfo vram --showuse --json`）→ torch** 顺序取
  整卡已用/总量/利用率，返回值带 `source`（说明数据来自哪个工具），结果缓存 0.5 秒（面板 1 秒轮询，不会每次都拉子进程）。
- `_vram()` / `status()` 统一改走 `gpu_probe()`，`/health` 现在同时给：`vram.used_gb / total_gb / free_gb / util / source`、
  `torch_allocated_mb`、`torch_reserved_mb`、`gpu_backend`（`cuda` / `rocm` / `cpu`，按 torch 版本判断）。
- `_warn_vram_fit`（加载时「装不装得下」的告警）也用同一口径，AMD 上不再误判。
- 面板：显存行改成两段 —— **整卡 X / Y GB · 本进程 分配/预留 MB · 来自 nvidia-smi|rocm-smi|torch**；
  GPU 名字后面的 `[cuda]` 换成按实际后端显示（AMD 上是 `[rocm]`）；利用率优先用探测到的值（AMD 机也能显示百分比）。
  网页版面板（`panel.py`）同口径。

**实测（本机 NVIDIA + 模拟 AMD 两种数据）**

服务端探测本身：

```
真实（本机 RTX 5070）: {'used_gb': 1.2, 'total_gb': 11.94, 'free_gb': 10.74, 'util': 5.0, 'source': 'nvidia-smi'}
模拟 AMD rocm-smi     : {'used_gb': 4.7, 'total_gb': 7.98,  'free_gb': 3.28,  'util': 37.0, 'source': 'rocm-smi'}
两边都没有只剩 torch : {'used_gb': 1.14, 'total_gb': 11.91, 'free_gb': 10.78, 'util': None, 'source': 'torch'}
```

面板渲染（喂假数据直接调 `apply_health`）：

```
NVIDIA 机：GPU: NVIDIA GeForce RTX 5070 [cuda]   显存: 3.4 / 11.9 GB   利用率: 41%
          整卡 3.4 / 11.9 GB · 本进程 2100/2600 MB · 来自 nvidia-smi
AMD 机   ：GPU: AMD Radeon RX 5700 XT [rocm]   显存: 6.1 / 8.0 GB   利用率: 97%
          整卡 6.1 / 8.0 GB · 本进程 5400/5900 MB · 来自 rocm-smi
```

> 服务端机上如果没有 `rocm-smi`（Windows 版 ROCm 常常不带），会自动退回 torch 口径，
> 这时行尾会显示 `来自 torch` —— 说明它给的是驱动报的可用显存，和 Adrenalin 可能有几十 MB 的差。

---

## 上一版 v1.7.57 —— LLM 文件夹下的 GGUF 读得到

**现象**：`vae` / `clip` 里的模型列表正常，`models\LLM` 下的 `.gguf` 在面板「GGUF 文本模型」里是空的
（或只有直接放在 `LLM` 根下的那几个，子目录里的一个都没有）。

**原因（GGUF 的扫描跟 CLIP/VAE 完全不是一套逻辑）**

1. 服务端 `llm.py` 的 `llm_dirs()` 只认 **固定文件夹名**（`LLM`/`llm`/`llm_gguf`/`gguf`/…），
   所以 `Qwen-LLM`、`LLM-models`、`models_llm` 这种名字一个都不算；
2. 就算认对了目录，`list_models()` 只往下 **2 层**（`dirpath[…].count(os.sep) > 2: continue`），
   `models\LLM\Qwen-Image-2.1\xxx.gguf` 这种套一层的还行，再深一层就没了；
3. 面板自己的本地扫描（服务未启动时显示的那份）更死：`_llm_dirs()` 最多 2 层，
   `_local_llm_view()` 只 `os.listdir` 那一层，子目录里的 GGUF 永远看不见；
4. 加完模型根目录后只刷新了 CLIP/VAE 列表，GGUF 两块要等下一次整体刷新。

**改法**

- `llm.py`：新增 `GGUF_MAX_DEPTH = 12`、`GGUF_DIR_HINTS = ("llm","gguf")`、`GGUF_SKIP_DIRS`（vae/clip/loras… 整棵跳过）。
  `llm_dirs()` 除了固定名，**名字里带 llm / gguf 的目录都算**；
  `list_models()` 改成从**模型根目录**直接递归（≤12 层）收 `.gguf`，仍按文件名分 `llm` / `llm_vision`（mmproj/vision/vit），
  条目名保持干净：路径里有 llm/gguf 目录就以它为基准（`LLM\xx.gguf` 还是显示 `xx.gguf`），没有就用相对根目录的路径。
  按绝对路径去重，同一个文件不会重复出现。
- `gui.py`：`_llm_dirs()` 深度 2 → 12、加「名字带 llm/gguf」的目录；`_local_llm_view()` 改成同样的递归扫 + 同样的命名规则；
  `scan_add()` 结束顺手刷新 GGUF 两块；日志多报一项：`当前列表：CLIP … 条，VAE … 条，GGUF … 个，mmproj … 个`。
- 服务端 `/models` 本来就转发 `llm.list_models()`，所以节点里的「文生图 / 图生图 / 视觉模型」下拉也随之变全。

**实测（临时目录造文件，面板真的不起来服务，只看本地扫描）**

```
造：LLM\ 下 2 个（模型 + mmproj）、LLM\Qwen-Image-2.1\ 下 1 个、Qwen-LLM\Qwen\Qwen-Image-2.1\ 下 1 个、LLM\a\b\c\d\ 下 1 个

点一次「添加目录…」之后：
GGUF  : ['Qwen-Image-2.1-PE-I2I.Q5_K_S.gguf', 'a/b/c/d/deep5.gguf',
         'Qwen-Image-2.1/Qwen-Image-2.1-T2I.Q5_K_S.gguf', 'Qwen/Qwen-Image-2.1/xxx.gguf']
mmproj: ['Qwen-Image-2.1-PE-I2I.mmproj-bf16.gguf']
日志  : 扫描根目录：vae 1、llm 4、llm_vision 1
日志  : 当前列表：CLIP 0 条，VAE 1 条，GGUF 4 个，mmproj 1 个
```

服务端侧 `LLMEngine.list_models()` 同样测过：`Qwen-LLM`（非标准目录）+ 4 层 + 7 层深的 gguf 都能列出，
`resolve("Qwen-Image-2.1-PE-I2I.Q5_K_S.gguf")` 仍能正确定位到绝对路径。

---

## 上一版 v1.7.56 —— 多层目录里的模型也读得出来

**现象**：模型文件放在 `models` 根目录下面套了好几层（比如
`models\vae\Comfy-Org\Qwen-Image\split_files\vae\qwen_image_2.1\xxx.safetensors`），
面板上点「扫描目录→加条目」/「添加目录…」都读不到，列表里没有它。

**原因（两处，都在扫描里）**

1. **深度写死 5 层**：`scan_roots` 的 `max_depth` 默认 5，`dirpath` 一旦超过 5 层就
   `dirs.clear()` 直接剪枝。上面那个例子正好第 6 层，文件被无声跳过 —— 这是「多层目录读不出来」的主因。
2. **只按文件夹名归类**：路径里没有任何一层叫 `vae` / `text_encoders` / `clip` 时
   （如 `models\Qwen\Qwen-Image-2.1\whatever\xxx.safetensors`），就算扫到了也会被判成「认不出」丢掉。
3. 顺带修掉一个会漏条目的点：界面按**文件名**去重，多层目录下不同路径的同名文件
   （`vae\a\model.safetensors` 与 `vae\b\deep\deeper\model.safetensors`）会被当成「已登记」跳过。

**改法**

- 新增常量 `ROOT_SCAN_MAX_DEPTH = 12`，`scan_roots(max_depth=12)` —— 12 层足够深，
  也不会把共享盘扫穿（`loras`/`checkpoints`/`diffusion_models` 等无关目录照旧整棵跳过）。
- `classify_rel` 增加**按文件名兜底**：路径里没有 `vae`/`clip` 之类的文件夹名时，
  文件名带 `vae`/`taesd`/`taef` → VAE，带 `clip`/`text_enc`/`t5`/`umt5`/`qwen3vl`/`qwen2.5vl`/`llava`/`gemma` → CLIP，
  `vision`/`vit`/`mmproj` → clip_vision。认不出的（例如 UNet）仍然不收，不会被误当成 CLIP。
- 界面去重改成按**绝对路径**；扫描时把「认不出类别」的文件也取回来，日志里点名报数：
  `另有 3 个文件没归类（Qwen/xxx.safetensors）：把它们的上级文件夹改成 vae / text_encoders / clip_vision 就会被读到`。
- 服务端 `_inject_root_models` 用的是同一个 `scan_roots`，所以服务端自己扫根目录也一起从 5 层放宽到 12 层。

**实测（临时目录里按真实布局造文件，脚本直接驱动面板）**

造的目录（全部在 `models` 根下）：

```
vae\Comfy-Org\Qwen-Image\split_files\vae\qwen_image_2.1_vae_bf16.safetensors        ← 第 5 层
vae\Comfy-Org\Qwen-Image\split_files\vae\qwen_image_2.1\qwen_image_2.1_vae_bf16_1.safetensors  ← 第 6 层（旧版漏）
text_encoders\Qwen\Qwen2.5-VL\qwen3vl_8b\qwen3vl_8b_int8_convrot.safetensors        ← 第 4 层
vae\a\model.safetensors  /  vae\b\deep\deeper\model.safetensors                     ← 同名文件
Qwen\Qwen-Image-2.1\whatever\qwen_image_vae.safetensors                             ← 目录名认不出，靠文件名
Qwen\Qwen-Image-2.1\whatever\qwen_image_2.1_int8_convrot.safetensors                ← UNet，不该被收
```

只点一次「添加目录…」（不点扫描按钮）的结果：

```
vae 条目：qwen_image_2.1_vae_bf16、qwen_image_2.1_vae_bf16_1、a_model、deeper_model、qwen_image_vae
clip 条目：qwen3vl_8b_int8_convrot
日志：扫描根目录：clip 1、vae 5
日志：另有 1 个文件没归类（Qwen/Qwen-Image-2.1/whatever/qwen_image_2.1_int8_convrot.safetensors）：把它们的上级文件夹改成 vae / text_encoders / clip_vision 就会被读到
日志：当前列表：CLIP 1 条，VAE 5 条
```

另测一条 11 层深的 `vae\l1\l2\…\l10\vae_12th.safetensors` —— 也读到了（深度上限 12）。
和注释里说的一样：**最稳的做法是把它们的上级文件夹改成标准名**（`vae` / `text_encoders` / `clip_vision`），
这样不管以后深度怎么变、文件名怎么起都能读到。

---

## 上一版 v1.7.55 —— 「添加目录…」之后就自动读模型（不用再手点扫描）

**现象**：在面板里点「添加目录…」选好 `models` 根目录，左边的 CLIP / VAE 列表还是空的，
必须再手点一次「扫描目录→加条目」才读得到里面的模型。

**原因**：`_after_roots_changed`（加/删根目录后的回调）只做了三件事：
`_save_cfg()` → 服务在跑就 `/reload` → `_refresh_models()`。**它从不扫描**，
所以根目录只是被记进配置，条目一条都没登记；只有 `scan_add` 那个按钮才会真去扫。

**改法**：

1. `_after_roots_changed` 现在直接调 `scan_add()` —— 加目录 / 删目录都会
   **保存 → 清掉失效的自动条目 → 按新根目录重新登记 → （服务在跑时）让服务端重读 → 刷新列表**；
2. 扫描结束在日志里报总数：`当前列表：CLIP 1 条，VAE 1 条（根目录 2 个）`，不用自己去数；
3. 加载界面时（`_loading`）不会触发扫描，避免打开面板就狂扫共享盘。

**实测（脚本直接驱动界面，点「添加目录…」→ 立刻看列表）**

```
--- 添加目录前
lst_roots: []
cfg.models: {"clip": [], "vae": [], "clip_vision": []}
界面列表 vae: ['（还没有条目：点「扫描目录→加条目」或运行 1_）']

--- 点「添加目录…」之后（不点扫描按钮）
cfg.models: {"clip": ["qwen3vl_8b_int8_convrot"], "vae": ["qwen_image_2.1_vae_bf16"],
             "clip_vision": ["clip_vision_qwen"]}
界面列表 clip: ['qwen3vl_8b_int8_convrot']
界面列表 vae : ['qwen_image_2.1_vae_bf16']
```

> 说明：扫描认的是**文件夹名**（`vae` / `clip` / `text_encoders` / `clip_vision` / `LLM`），
> 最多往下 5 层，并且会整棵跳过 `loras / checkpoints / diffusion_models / controlnet` 这些无关目录。
> 如果你的文件放在 `models` 根目录正下方而文件夹名不规则，文件名里带 `vae` 的会归到 VAE，
> 其余认不出来的就不收 —— 这种就把它们挪到 `vae\` / `text_encoders\` 下面。

---

## 上一版 v1.7.54 —— exe 不再生成任何文件（设置进注册表）+ 删根目录后模型列表跟着删

### 一、模型列表不跟着删（两个原因都修了）

**现象**：在面板里把某个「模型根目录」点「移除选中」删掉（或者把模型文件/整个目录删了），
左边 CLIP / VAE 列表里那些条目还挂着，有的标 `✗ 文件缺失`，有的甚至还能选。

**原因**：扫描出来的条目只加不减。

1. 面板 `scan_add` 把扫到的文件写成 config 的 `models` 条目，从来不做反向清理；
2. 服务端 `Backend._inject_root_models` 也是只往 `models_cfg` 里塞，`/reload` 只加不删。

所以 `config.json` 里的条目会越攒越多，跟真实目录彻底脱钩。

**改法**：给「扫目录自动登记」的条目打标记 `"auto": true`（老条目按 note 前缀
`根目录扫描` / `目录扫描` 兼容识别），然后新增 `config.prune_auto_models(models, roots)`：

| 条目情况 | 处理 |
|---|---|
| 文件已不存在 | 删 |
| 文件不在当前任何「模型根目录」下（来自已移除的根目录） | 删 |
| **手写条目**（没有 auto 标记、note 也不是扫描来的） | **绝不动** |

调用点四处，任何一条路径触发都会同步：

- 面板 `_collect_cfg()`（保存/退出前）→ 写进存储的清单永远和真实目录一致；
- 面板 `scan_add()` 扫描前 → 先清失效条目再重扫；
- 面板「移除选中」→ `_after_roots_changed` → 保存+重扫；
- 服务端 `_inject_root_models()`（含 `/reload`）→ 服务端缓存也一起清。

日志里会说清楚：`[服务端] 已移除自动条目 vae/other_vae（来自已移除的模型根目录）`。

### 二、exe 设置与生成文件「内置」到应用里

**要求**：exe 设置后不要再创建新文件。原来的 exe 会在自己旁边生成
`config.json`、`gui_state.json`、`_python.txt`、`_comfy.txt`、`_smoke.txt`。

**改法**（新增 `model_server/store.py`）：exe 模式（`sys.frozen`）下这些内容**全部写进 Windows 注册表**：

```
HKEY_CURRENT_USER\Software\RemoteCLIPVAE
   config.json       ← 监听地址/端口/ComfyUI 路径/模型根目录/模型条目…
   gui_state.json    ← python 路径、窗口位置
   _python.txt / _comfy.txt / _smoke.txt
```

- **exe 目录里只剩 exe 本身**，不会再出现任何配置文件或记录文件；
- 默认配置内置在 exe 里（注册表没有就用内置默认值，不会先生成一份模板文件）；
- **启动服务端也不再写配置**：界面把当前配置序列化成 JSON，通过环境变量
  `RCLIPVAE_CONFIG_JSON` 传给服务端子进程，`app.py` 读到就直接用（日志：
  `[B] 配置由界面直接传入（环境变量），不读也不写任何配置文件`）；
- 面板改设置后让服务端重载：新增 `POST /reload`，body 就是 `{"config": {...}}`
  （GET `/reload` 仍然保留给源码版读文件）；
- 源码版（直接 `python model_server/gui.py`）行为不变，还是读写同目录文件，方便调试。

**唯一还落盘的东西**：点面板「装 Vulkan 版」时下载的 `llama-cpp-python` wheel（约 42 MB），
存在 `%LOCALAPPDATA%\RemoteCLIPVAE\wheels\`。这是第三方二进制依赖，没法塞进 exe，而且留着才能续传/换机复用；
不点那个按钮就不会产生。

**关于临时目录**：单文件 exe 运行时本来就会解到 `%TEMP%\_MEI…`，退出即删（PyInstaller 固有行为），
exe 所在目录永远是干净的。

**实测**

```
# 1) 单文件 exe（新版）放进空文件夹跑 --smoke
exit=True
文件夹内容: [RemoteCLIPVAE服务端.exe]        ← 一个文件都没有多出来
注册表值: config.json, gui_state.json, _smoke.txt
smoke 报告: settings : Windows 注册表 HKCU\Software\RemoteCLIPVAE
            config源  : 注册表/内置默认（不落盘）

# 2) app.py 从环境变量吃配置 + POST /reload（工作目录故意没有 config.json）
[B] 配置由界面直接传入（环境变量），不读也不写任何配置文件
health ok=True mock=True
POST /reload ok=True models={"clip":0,"vae":1,"clip_vision":0}
工作目录: []                                  ← 空

# 3) 真实 Backend：移除根目录 / 删文件 → 列表跟着消失
1) 两个根目录都在：vae = ['qwen_image_2.1_vae_bf16', 'other_vae'] | clip = ['qwen3vl_8b']
[服务端] 已移除自动条目 vae/other_vae（来自已移除的模型根目录）
2) 移除 RootB 后：vae = ['qwen_image_2.1_vae_bf16'] | clip = ['qwen3vl_8b']
[服务端] 已移除自动条目 vae/qwen_image_2.1_vae_bf16（文件已不存在：qwen_image_2.1_vae_bf16.safetensors）
3) 删掉 vae 文件后：vae = []
4) 手写条目仍在： ['manual_vae']
```

**换机迁移提醒**：设置现在在注册表里，不再跟着 exe 文件夹走。要搬设置，可以在新机上重新选一遍路径
（3 个框：ComfyUI 路径、python、模型根目录），或者导出注册表
`HKEY_CURRENT_USER\Software\RemoteCLIPVAE` 再导入。

---

## 上一版 v1.7.53 —— 本机 VAE 出图糊的真因：`keep_vision` 被算成 `True`（视觉 token 插了两次）

**现象**：接本机 VAE 时比例对、但整张糊/满屏颗粒；接远程 VAE 时出图正常（只是 1024×1024）。
所以问题不在 VAE 本身，也不在采样器 —— 是**条件被插了两份**。

**真因**：`RemoteCLIPImageEncode` 发 RPC 时，`keep_vision` 是这么推的：

```python
"keep_vision": not bool(vae_model)      # v1.7.52 及之前
```

本机 VAE 那条路为了让服务端别加载 VAE，`vae_model` 传的是空串（服务端日志里 `vae=` 空着那条），
于是 `keep_vision=True`；而本机这边已经把参考图编成 `reference_latents` 挂到条件上了。
结果服务端在 tokenize 时**保留了 vision token**，本地又**额外挂了一份 reference_latents** →
同一张参考图在条件里出现两次（vision 特征 + latent），模型自注意力被打乱 → 糊/重影/颗粒。
远程 VAE 那条 `vae_model=qwen_image_2.1_vae_bf16`，`keep_vision=False`，只有一份 latent，所以正常。

**改法**：`keep_vision` 不再猜，显式传。

```python
def encode_with_images(self, text, images, vision_pixels=..., vae_model="", keep_vision=None):
    "keep_vision": bool(keep_vision) if keep_vision is not None else (not bool(vae_model))
```

节点里只要本机编出了 `ref_lats`，就 `keep_vision=False` 传下去（和原生 `TextEncodeQwenImageEditPlus` 里
`keep_vision = len(ref_latents)==0` 完全一致）：有 latent 就不要 vision token。

**顺手按你的要求删掉两样**：

| 删了什么 | 说明 |
|---|---|
| 第 3 个输出「参考图 latent」 | 节点回到 **2 个输出（正向条件 / 负向条件）**。采样 latent 用你自己的 VAE 编码节点编同一张参考图 |
| 「画布尺寸」下拉 | 尺寸统一由「参考图最大像素」决定（参考 latent 与视觉 token 同一档：1MP ≈ 1000×1000） |

**尺寸也统一了**：本机 VAE 编 reference_latents 用和远程 VAE 完全同一套算法
（`sqrt(px²·ratio)` 等比取整到 32 的倍数）。所以两条路产出的 latent 尺寸一致：

| 参考图 1422×1920 | 参考 latent | 备注 |
|---|---|---|
| 参考图最大像素 = 1.0 | 864×1152 → `(1,64,72,54)` | 与远程那条的 `1024×1024` 同档（≈0.995MP） |
| 参考图最大像素 = 0（保原尺寸） | 1408×1920 → `(1,64,120,88)` | 10560 patch，显存/细节压力大 |

**怎么跑（现在只有一条正确姿势）**：

1. `远程 CLIP 图文编码` 的 `vae` 口：接远程 VAE 加载器 **或** 本机 VAE 加载器，都行；
2. KSampler 的 `latent`：用你自己的 VAE 编码节点编**同一张**参考图；
3. `denoise` 0.55~0.8；
4. 两侧尺寸保持同一档（都 1MP 档最稳；差太多会糊/颗粒）。

实测（测试机，假 VAE 64ch/1÷16 + 假 RPC）：

```
--- A. 接本机 VAE（vae_model 为空）
图 1 张 | 参考图最大像素=1MP | keep_vision=False | vae=无 | 本机 vae 已编 reference_latents×1
输出个数: 2 | 服务端 keep_vision = False | vae 字段 = ''
条件里 reference_latents: (1, 64, 72, 54)
--- B. 接远程 VAE 代理（vae_model 有值）
输出个数: 2 | 服务端 keep_vision = False | vae 字段 = 'qwen_image_2.1_vae_bf16'
```

两条路 `keep_vision` 都是 `False`、都只有 2 个输出 —— 双重插入这条路已经被堵死
（v1.7.51 章节里“画布开到 2.7MP 撑不住”的说法不成立，已按本条为准）。

---

## 上一版 v1.7.52 —— 新增「画布尺寸」，第 3 输出默认与原图等大

问题：接了本机 VAE 时，第 3 个输出「参考图 latent」受「参考图最大像素」限制，
出图变成 864×1152，跟原图 1422×1920 对不上（接远程 VAE 那条路不受影响，所以那边是好的）。

改法：**把“视觉 token”和“画布”两个尺寸彻底分开**，新增一项下拉 `画布尺寸`：

| 选项 | 第 3 输出 latent | 出图 |
|---|---|---|
| **原图尺寸（出图与原图一致）** ← 默认 | 按原图尺寸编（只取整到 VAE 的缩放比） | 与原图几乎等大 |
| 按参考图最大像素（缩小画布省显存） | 跟 MP 那档缩 | 变小（省显存/低配机器用） |

「参考图最大像素」现在**只约束服务端视觉 token**，不再偷偷改画布。

**实测（模拟你那个 64ch / 1÷16 的 VAE + 1422×1920 原图）**

```
默认（原图尺寸）        → 编码 1408×1920 → latent (1,64,120,88) → 出图约 1408×1920
按参考图最大像素 1MP    → 编码 864×1152  → latent (1,64,72,54)   → 出图约 864×1152
节点整体跑：第3输出 latent (1,64,120,88)，条件里的 reference_latents 同尺寸 (1,64,120,88)
py_compile = 0
```

> 1422 → 1408 少 14 px，是因为这个 VAE 的空间缩放是 1/16，宽高必须是 16 的倍数
> （原生 VAEEncode 也会裁成 1408）。要像素级等大，后面接一个 `Image Crop` 裁回 1422×1920 即可。

---

## 本次更新 v1.7.51 —— 颗粒噪点的真因找到：**画布开到 2.7MP，模型撑不住**

用户实测日志（决定性证据）：

```
[远程CLIP图文编码] 本机 vae 编码第 1 张参考图：1422×1920 → 1416×1920 → latent (1, 64, 120, 88)
[远程CLIP图文编码]   该 latent 统计：std=4.267 min=-18.88 max=20.38
[INFO] Model QwenImage21 prepared for dynamic VRAM loading. 6920MB Staged.
```

逐条读：

- VAE 是 `qwen_image_2.1_vae_bf16.safetensors`，**64 通道 / 1÷16 下采样** —— 和主模型 `qwen_image_2.1_int8_convrot` 是配套的，**VAE 没问题**；
- `std=4.27 / ±20` 是这个 64 通道 VAE 的正常尺度（前一版“std≈1”的提示语对不同 VAE 会误伤，已改掉）；
- 真正的问题：参考 latent 是 **120×88 = 10560 个 patch**。1024×1024 一档才 4000~4200。
  画布大了 2.6 倍，模型在每个 patch 上分到的“表达预算”不够，只能猜 → **纹理全变颗粒、暗部发脏**，而构图和人物大小是对的。

**改法**

1. 「参考图最大像素」重新**同时决定视觉 token 和画布**（两者必须同尺寸，之前拆开用了两套算法才出问题）：
   `1422×1920` + 1.0MP → `864×1152` → latent `54×72 = 3888 patch`（这个量级才稳）。
   控件填 0 = 用原图尺寸（不推荐）。
2. 日志新增 **patch 数告警**：超过 6000 直接提示「画布偏大，把参考图最大像素调到 1.0」。
3. latent 统计那行改成中性提示（不同 VAE 尺度本来不同，只作对比用）。

**实测（模拟你那个 64ch / 1÷16 的 VAE + 1422×1920 原图）**

```
px=0（原图）      → 编码 1416×1920 → latent (1,64,120,88) patch=10560  ← 会打“画布偏大”告警
px=1.0MP（默认）  → 编码 864×1152  → latent (1,64,72,54)  patch=3888
px=0.6MP          → 编码 672×896   → latent (1,64,56,42)  patch=2352
py_compile = 0
```

**你只要做一件事**：把「参考图最大像素」保持 **1.0**（默认），并把第 3 个输出「参考图 latent」接到 KSampler。
如果还想更好，把步数从 20 提到 28~30（现在日志里是 `20/20 [5.78s/it]`，20 步对 int8/NVFP4 量化模型偏少）。

另外新增一个排障节点 **「VAE 自检 (VAE Self Check)」**：接 `vae` + `图像`，一键做 encode→decode 往返，
输出「往返还原图」和一段报告（PSNR、最大误差、latent 形状/std、VAE 的通道数/缩放比/dtype），
以后怀疑 VAE 直接跑它，不用再猜。

---

## 本次更新 v1.7.50 —— 参考 latent 加自检统计（用来定位“糊/噪点”出在哪一层）

同一个现象（构图、人物大小都对，但整张图满是高频噪点、椅背纹理变成颗粒）继续查。
这版在节点日志里补两行关键数据，用来判断问题在 VAE、采样还是我们这一步：

```
[远程CLIP图文编码] 本机 vae 编码第 1 张参考图：630×834 → 624×832 → latent (1,16,78,104)
[远程CLIP图文编码]   该 latent 统计：std=1.02 min=-4.8 max=5.1
                         （同一套 VAE 编出来的值一般 std≈1、范围 ±5 上下；
                           差得远说明这个 VAE 不是该模型配套的那个）
```

对上表怎么看：

| 日志里看到 | 结论 |
|---|---|
| `latent (1,16,78,104)` 且 KSampler 画布是 630×834（latent 78×104） | 尺寸**对上了**，问题不在尺寸 |
| `std` 约 1、`min/max` 约 ±5 | VAE 是配套的，问题在**采样参数/模型** |
| `std` 明显偏离（如 0.2 或 5 以上）或通道数不是 16 | VAE 与该模型**不配套**，换配套的 VAE |

同时附一个 30 秒的“甩锅测试”，能直接判定是不是插件的问题：

1. **VAE 往返测试**：`LoadImage → 原生 VAEEncode → 原生 VAEDecode → PreviewImage`（中间不接任何采样）。
   - 出来干净 → VAE 没问题；出来就是颗粒 → VAE/精度问题，与插件无关。
2. **改图测试**：`原生 TextEncodeQwenImageEdit`（自带那个节点）→ `KSampler(denoise 0.6)` → `VAEDecode`，latent 用上面第 1 步的 VAEEncode。
   - 出来一样颗粒 → 采样参数/模型问题（步数太少、cfg 太低/太高、fp8 量化过头）；
   - 出来正常 → 再用我们的节点替换掉那个原生编码节点对比，把日志贴出来。

---

## 本次更新 v1.7.49 —— 去掉所有「模型限制」，模型/VAE/类型换成什么都随你

原则：插件不再替用户判断“该用哪个模型”，一律不拦、只提示。

| 原来会拦的地方 | 现在 |
|---|---|
| 「模型类型」下拉写死 14 项 | 导入时**动态合并 ComfyUI 自己的 `CLIPType` 枚举**（本机实测 40 项 → 下拉 43 项，缺 0 项），以后 ComfyUI 加新类型也能选 |
| `VALIDATE_INPUTS` 遇到占位符 → 画布弹「无效输入」 | 队列校验**一律放行**；占位符/空值改成执行阶段一句中文提示（`_require_model`） |
| 本机 VAE latent 不是 16 通道 → 直接抛错 | **不再拦**，只在“看着不像配套”时打一行提示（通道数/缩放比都从你实际接的 VAE 读，不同模型本来就不一样） |
| 参考 latent 占位固定 16 通道 | 改成跟着你接的 VAE 的 `latent_channels` 走 |
| 「远程 VAE 解码/编码」只认远程 VAE，接本机 VAE 直接报「vae 输入无效」 | **本机 VAE 也能用**：检测到 `decode()`/`encode()` 就在本机做（等价自带 VAEDecode/VAEEncode），日志说明 |
| 服务端 `clip_type` 写法不认识就失败 | 先按名字忽略大小写匹配 → 再按关键字猜（qwen/flux/sd3/sdxl/wan/…）→ 实在不行才列出可用项；显式指定与权重不符时**只提醒不覆盖** |
| 日志里“必须加载 qwen-image-edit 权重” | 改成“权重请用与你 CLIP/VAE 同一套的那个版本” |

**实测（本机 ComfyUI 环境）**

```
1) ComfyUI CLIPType 40 个 / 下拉 43 个 / 缺：无
2) _validate_model("（连接失败，检查服务端）") -> True；空串 -> True；任意名字 -> True
   执行阶段提示：「模型」还没选：点节点上的「🔄 刷新模型列表」选一个服务端已有的CLIP条目…
3) 接 4 通道的 VAE：不再报错，照常返回 latent (1,4,78,57)，图文编码输出 3 个
4) 远程 VAE 解码节点接本机 VAE：本机解码出图 (1,64,64,3)；编码出 latent (1,4,8,8)
nodes.py / backend.py py_compile 均 = 0
```

---

## 本次更新 v1.7.48 —— 修「本地 VAE 出图人物变小、椅子扭曲、满屏噪点」

**症状**：参考图 458×627 很清晰，出图变成人物缩到中间一坨、椅子纹理拉成皱纹、整体像高噪点低分辨率。

**真因（两条叠加）**

1. **参考 latent 尺寸错了**：v1.7.47 借用了「参考图最大像素」（0.4MP）去算参考 latent 尺寸，
   458×627 只有 0.29MP → 被**放大**到 544×736。参考画面比 KSampler 的画布大 →
   模型按比例把主体缩小、把椅子拉伸铺满 → 出图就是这个样子。
   那栏本来只是给服务端**视觉 token** 用的，不该掺进 latent。
2. **画布尺寸靠人手动对齐**：KSampler 的 latent 要是 EmptyLatentImage 或尺寸对不上的 VAE 编码，
   reference_latents 和画布永远对不齐，怎么调 denoise 都糊。

**改法**

- 参考 latent **改用原图尺寸**（只对齐到 8 的倍数、只缩不放，最多掉 7px），与原生
  `TextEncodeQwenImageEditPlus` 的语义一致 → 参考画面 = 画布比例，人物不再缩。
- 新增**第 3 个输出「参考图 latent」**（LATENT）：直接把它接到 KSampler 的 latent 输入端，
  denoise 0.55~0.8，画布尺寸与参考画面天然一致，不用再手填尺寸。
- 新增**VAE 身份校验**：latent 不是 16 通道、或空间缩放不是 1/8，直接中文报错
  「接进来的这个 VAE 不是 Qwen-Image 的 VAE（比如接了 SD / Flux 的 VAE）」——
  接错 VAE 也会出这种噪点图，现在会当场拦住。
- 日志打印每张图的 `原图 → 编码尺寸 → latent 形状`。

**实测（伪造 rpc + 假 qwen VAE，用你那张 458×627 的图）**

```
A) qwen VAE、MP=0.4：vae.encode 收到 (1,624,456,3) → latent (1,16,78,57)
   第3输出 samples=(1,16,78,57)，reference_latents 已挂到条件，返回 3 个输出
B) 改成 MP=1.0：vae.encode 收到 (1,624,456,3) —— 尺寸不再随 MP 变（旧版会变）
C) 接 4 通道 SD 的 VAE：被拦 → 「本机 VAE 编出来的 latent 是 4 通道（Qwen-Image 需要 16 通道）…」
D) 不接 vae：第3输出是占位空 latent（带 remote_clipvae_placeholder 标记，日志提示别接）
py_compile = 0
```

**你必须做的一步**：把本节点第 3 个输出 `参考图 latent` 接到 KSampler 的 `latent_image`，
denoise 0.55~0.8；VAE Loader 用 `qwen_image_vae.safetensors`（不要 Flux/SD 的）。

---

## 本次更新 v1.7.47 —— 接「本机 VAE」也按参考图改（不再变成重绘）

**原因**：`reference_latents` 只在 `vae` 是**远程** VAE（`RemoteVAE` / 老字典句柄）时才由服务端编。
接的是**本机原生 VAE 加载器**时，节点认为“不是我的 VAE”，既不编 latent 也不挂条件 →
服务端只剩视觉槽、采样器拿到纯噪声 latent → 于是变成“重绘”。

**改法**：新增 `_vae_ref_latents()` + `_attach_ref()`，完全照 `comfy_extras/nodes_qwen.py::
TextEncodeQwenImageEditPlus` 的做法，**在本机把参考图编成 reference_latents**：

```
图片按 sqrt(参考图最大像素)² 等比缩放到 32 的倍数（与原生节点同一套算法，lanczos）
→ 本机 vae.encode(去 alpha 的 BHWC) → reference_latents
→ 追加到正向 + 负向条件的 dict 里（append=True 等价）
→ 同时把发给服务端的 vae 置空（服务端不再按名字加载 VAE，避免重复编一遍）
→ 有 reference_latents 时 keep_vision=False（避免 vision 槽 + latent 双重插入，出图糊）
```

所以现在三条路径一致：

| vae 输入口 | reference_latents 谁编 | 结果 |
|---|---|---|
| 远程 VAE 加载器 | 服务端（按名字加载那台机器上的 VAE） | 改图 |
| **本机 VAE 加载器（新）** | **本机**（`vae.encode`，仅本机显存/内存） | 改图 |
| 没接 | 没人编 | 只有视觉槽 → 基本等于重画（控制台会警告） |

本地原生 **clip** 那条分支（clip 直接接原生 CLIPTextEncode 用的那种）也一并挂上 reference_latents。

**实测（伪造 rpc + 假原生 VAE，看真正发给服务端的参数和返回的条件）**

```
A) clip=远程 + vae=本机原生：
   vae=无 | 本机 vae 已编 reference_latents×1
   请求里 vae='' （服务端不再加载）、vision_pixels=400000
   正向条件带 reference_latents: True，张量 [1,16,64,96]；负向条件也带: True

B) clip=远程 + vae=远程：请求里 vae='qwen_image_vae.safetensors'，客户端不重复挂

C) 没接 vae：vae='', 不挂 latent（照旧警告）
py_compile(nodes.py)=0
```

---

## 本次更新 v1.7.46 —— 「接了参考图却像重新画一张」：把两条路径的区别说清楚（并给出检查清单）

**为什么会出现“重新生成”**：Qwen-Image-Edit 的“改图”要靠**两样东西同时到位**，缺一条就退化成重画：

| 要素 | 谁提供 | 缺了会怎样 |
|---|---|---|
| `reference_latents`（参考图的 VAE 编码，挂到条件上） | 本节点的 `vae` 输入口 | 模型看不到原图结构，只能按文字重画 |
| 起始 latent（KSampler 的 `latent`） | `远程 VAE 编码`（同一张参考图）或同尺寸 `EmptyLatentImage` | 用噪声 latent + denoise=1.0 → 100% 重画 |
| `qwen-image-edit` 权重 | UNet/模型加载器 | 文生图权重不认 `reference_latents`，出图跟参考图无关 |

**这一版做了什么**（不改你的工作流，只把沉默变明确）：

1. 有图但**没接 vae** → 控制台直接打出警告，说清“现在只塞视觉槽、没有 reference_latents、结果等于重画”，
   并列出三步改法（接 VAE、latent 用 VAE 编码 + denoise 0.55~0.85、采样尺寸跟参考图一致）。
2. 有图且**接了 vae** → 打出“已按 Qwen-Image-Edit 编、reference_latents 已挂”，并提示剩下两条（latent 来源、
   denoise 区间、edit 权重）。
3. 两个关键输入口的 tooltip 也补了这段说明（`vae` / `图片1…图片10`）。
4. 本地原生 clip 分支同样打一条说明（reference_latents 由原生节点自己挂）。

**实测（伪造 rpc，看 vae 是否真的发出去）**

```
A) 有图、没接 vae：
   [远程CLIP图文编码] 图 1 张 | 参考图最大像素=0.4MP（400000 px）| vae=无
   ⚠ 只接了图、没接 vae：…没有 reference_latents … 结果基本等于“照着描述重新画一张”
   → 请求里 keep_vision=True、vae=""

B) 有图、接了 vae：
   [远程CLIP图文编码] 图 1 张 | … | vae=qwen_image_vae.safetensors
   已按 Qwen-Image-Edit 的方式编：参考图同时进视觉槽 + VAE，reference_latents 会挂到条件上
   → 请求里 vision_pixels=400000、vae="qwen_image_vae.safetensors"（服务端据此挂 reference_latents）
py_compile(nodes.py)=0
```

---

## 本次更新 v1.7.45 —— 负向提示词里「不要改变…」是反的，改成词条

**你说得对**：负向提示词只管「不想要什么」，写进一句「不要改变人物的面部特征、发型、姿势与背景布局」
等于在负向通道里再否定一次 —— 双否定绕回来就成了「**要**改变」，语义正好反了。

**改了两处**

1. 系统提示词新增硬规定（三种分支都带）：
   > negative_prompt 只写「希望画面里避免出现的现象/物件」的名词短语，用顿号分隔，
   > 例如：面部特征被改变、发型被改变、姿势与构图变化、背景布局变化、多余道具、文字水印。
   > 严禁以「不要…」「别…」「禁止…」「请勿…」「避免…」开头，也不要写成完整句子
   > （写「不要改变人物的面部特征」等于在负向里反向要求，是错的）。
2. 出结果后再兜一层 `_clean_negative()`：把每条以 `不要/别/请勿/切勿/勿/禁止/严禁/避免/不应该/不得/
   不能/不可以/不该` 开头的词条把前缀摘掉、按 `，、；` 拆开、去重、用顿号重连。
   本身已经是词条（如 `低清晰度, 变形, 多余手指`）时**一个字都不动**。
   日志：`[提示词优化] 负向提示词里有 3 条是「不要…」句式（在负向里等于反向要求），已改成词条：…`

**实测**

```
原文 : 不要改变人物的面部特征、发型、姿势与背景布局，不要改变黑色丝袜与高跟鞋，不要新增任何道具或文字。
改后 : 改变人物的面部特征、发型、姿势与背景布局、改变黑色丝袜与高跟鞋、新增任何道具或文字

词条式「低清晰度, 变形, 多余手指, 文字水印」→ 原样不动（未被改动: True）
单测: '别把头发变长，禁止新增道具' → '把头发变长、新增道具'
      '不应该改变姿势'            → '改变姿势'
      '模糊、噪点'                → 原样（0 处改动）
system 含负向规则: True
py_compile(nodes.py)=0
```

---

## 本次更新 v1.7.44 —— 「透明背景」关着，输出却一直带透明：现在两头都堵住

**现象**：开关没开，优化出来的提示词里还是写着「同时按照用户要求将背景处理为完全透明，并以纯白色背景呈现…」。

**为什么**（两条一起犯）

1. 系统提示词里有一句「若用户要求透明背景，…明确说明背景完全透明」——GGUF 模型看到这句话就当成
   默认要求，不管开关。
2. 你多半把上一轮的输出当成本轮的「用户提示词」粘回去了，里面本身就带「透明背景」，模型顺着写。

**改了三处**

1. 系统提示词里的那句「若用户要求透明…」直接删掉，改成**按开关给硬策略**：
   - 开：「背景必须是完全透明的（保留 alpha），主体之外不出现环境、地面、墙面与投影；不要写成纯白/纯色背景。」
   - 关：「背景一律按参考图（没有参考图就按正常实景）原样保留；**严禁出现透明背景 / alpha / 抠图 /
     去背景 / 纯白背景 / 白底这类要求，也不要自作主张把背景改成透明或纯色**。」
   （三种系统提示词——文生图 / 图生图 / 反推——都带这条。）
2. 出结果后再扫一遍（`_scrub_transparency`）：开关关着、且你的用户提示词里没有「透明 / alpha / 抠图」
   这类词时，把命中这些词的**分句整条删掉**（先按 `。；！？` 切，漏网再按 `，、` 切），
   正负提示词都扫。日志：`[提示词优化] 透明背景=关：已从输出里去掉 2 处「透明/抠图/白底」描述（模型自作主张加的）`
3. 保护：如果删完什么都不剩（说明整条都是透明内容），直接中文报错，点明「你的用户提示词里粘了上一轮
   带透明背景的输出，或参考图本身是透明底 PNG」，而不是把一个空提示词丢给采样器。

**例外（有意保留）**：你自己在「用户提示词」里写了「背景要透明」，开关关着也照你说的走——不删、不拦。
判断方式就是扫用户提示词里有没有 `透明 / alpha / 抠图 / 去背景 / 白底`。

**实测（伪造 rpc，喂的正是你截图那条带透明的输出）**

```
1) 开关=关，用户词没提透明 → 删掉 2 处，结果无透明字，负向里那个“透明背景”也删了
2) 开关=关，用户词写了“背景要透明” → 不删，system 走透明策略
3) 开关=开 → 不删（该保留）
4) 纯文本分支(文生图) 开关=关 → 同样删干净
5) _scrub_transparency 单测：'A无关键词；同时将背景处理为完全透明…。B保留人物。' → 'A无关键词；B保留人物'
py_compile(nodes.py)=0
```

---

## 本次更新 v1.7.43 —— 「clip」输入口接上后，本节点的模型设置自动作废（不再弹「2 无效输入」）

**现象**（你截图那个）：`远程 CLIP 图文编码` / `远程 CLIP 文本编码` 左边「clip」「vae」接上了
`远程 CLIP 加载器`，但节点下面那排「服务端显卡 / 计算设备 / 服务端地址 / 模型 / 模型类型」还是默认值
（服务端连不上时是 `（连接失败，检查服务端）`），点运行就红：**错误 · 2 无效输入**。

**为什么是 2 条**：编码节点的 `VALIDATE_INPUTS` 只认 `模型`、`模型类型` 两个参数，返回一句错误时
ComfyUI 会**按这两个参数各算一次**，所以同一个原因显示成 2 条。

**改了三处**

1. `VALIDATE_INPUTS` 加 `input_types` 参数：只要 `clip` 这个输入口是连着的，直接返回 `True` ——
   模型名/类型/地址全部由上游加载器决定，本节点的下拉即使是占位符也不参与校验。
   （`单个 vae` 连着但没连 clip 时仍照旧校验，因为 vae 加载器不带 CLIP 模型名。）
2. `encode()` 里接上 clip 时打印一行日志，说明用的是谁的模型：
   `[远程CLIP图文编码] 用输入端口「clip」的模型：xxx @ http://…（本节点的地址/模型设置已忽略）`
3. 前端 `refresh.js`：接上 clip 就把这 5 栏 +「🔄 刷新模型列表」按钮一起收起（收起方式加固：
   `type="hidden"` + `computeSize=[0,0]` + 不画不响应鼠标 + 重排 `last_y` 后重算节点尺寸），
   断开连线立刻还原。

**实测（用 ComfyUI 自己那套 `execution.validate_inputs` 跑真队列校验）**

```
[1] 文本编码 VALIDATE_INPUTS(模型=占位符, input_types={"clip":"CLIP"}) → True
[1] 同上但不接 clip                                              → 中文报错（保护性校验仍在）
[1] 图文编码 VALIDATE_INPUTS(模型=占位符, input_types={"clip":"CLIP"}) → True

[4] 加载器填真模型名、编码节点自己那栏仍是占位符：
    文本编码 node1 → valid=True errors=[]
    图文编码 node1 → valid=True errors=[]

[2] 加载器也连不上服务端（模型=占位符）：
    RemoteCLIPLoader → custom_validation_failed ×2（就是以前的「2 无效输入」，属正常：它没法知道有什么模型）
```

`py_compile(nodes.py)=0`，`node --check refresh.js=0`。

用法：接上加载器后，下面那排设置会自己消失，不用再管；想改模型就去改「远程 CLIP 加载器」那栏。

---

## 本次更新 v1.7.42 —— 反推/优化都按「系统提示词 + 用户提示词」发；开关文案改短

**行为**：不管「是否反推」是开还是关，送给模型的内容都是同一套结构——

```
system : 系统提示词（开=反推用的 SYS_REV / 关=图生图 SYS_I2I、文生图 SYS_T2I）+ 语言规则
user   : 你写的「用户提示词」，原样发出去
```

以前开反推会把用户提示词**替换**成一句固定的「请反推这张图的提示词。」，你写的主体/材质/禁止水印等要求
只被拼在后半句里，模型容易当噪声忽略。现在改成：

- 用户提示词一个字不改（要做反推时，系统提示词里会明确写着“这是反推任务、用户提的关注点必须体现在结果里”）；
- 只有用户提示词**留空**时才补一句「请反推这张图的提示词。」（模型不能收到空 user 消息）；
- 勾了「透明背景」且是反推时，在用户提示词后追加一行「背景要求透明。」。

**界面文案**（正对应你第二张截图）：

```
是否反推   [ ⬤ ]
           开：反推图
           关：系统优化提示词
```

tooltip 也一并改成：
> 开 = 反推图（必须有参考图）：按系统提示词把图里的内容反推成提示词；关 = 系统优化提示词：有图走图生图改写、无图走文生图。两种情况下发给模型的内容都是「系统提示词 + 用户提示词」，你写的用户提示词会原样一起发出去，不会被替换或丢掉。

### 本版实测（伪造 rpc，比对真正发出去的 system / user）

```
label_on = 开：反推图      label_off = 关：系统优化提示词

开关=开：user 文本 == 用户输入（'要突出金属质感，别写水印'）✓  system=反推系统提示词
开关=关：user 文本 == 用户输入（同一句）✓                    system=图生图系统提示词
开 + 空用户提示词：user = '请反推这张图的提示词。'
开 + 透明背景：user = '重点写主体\n背景要求透明。'
py_compile(nodes.py) = 0
```

---

## 本次更新 v1.7.41 —— 提示词优化：有图走「图生图模型」，无图走「文生图模型」

规则改成严格二分，不再有含糊地带：

| 有没有接参考图 | 用哪个 GGUF | 视觉模型(mmproj) | 系统提示词 |
|---|---|---|---|
| **接了图**（含反推） | `图生图模型` | 必须选，否则中文报错 | 反推→`SYS_REV`；不反推→`SYS_I2I` |
| **没接图** | `文生图模型` | **不加载**（即使下拉里选了也忽略，纯文本用不到视觉塔） | `SYS_T2I` |

配套改动：
- 日志直接标出分支和实际模型，一眼能核对：
  `[远程提示词优化] RemoteCLIPVAE | 分支=图生图 | model=I2I.gguf | mmproj=mm.gguf | 图 1 张 …`
  `[提示词优化-本地] 分支=文生图 | model=T2I.gguf | mmproj=无（纯文本） | 图 0 张 …`
- 报错点名具体那一栏：「「文生图模型」还是占位符…」「「图生图模型」还是占位符…」，
  本地模式还会带上「当前分支是 X（接了/没接参考图）」。
- 无图分支不再把 mmproj 传给服务端：省掉视觉投影那份显存，也避免服务端为你没用的视觉塔做前置检查。

### 本版实测（伪造 rpc，检查真正发出去的 model / mmproj）

```
有图 + 不反推 → model=I2I.gguf, mmproj=mm.gguf         ✓
有图 + 反推   → model=I2I.gguf, mmproj=mm.gguf         ✓
无图（视觉模型仍选了）→ model=T2I.gguf, mmproj=''       ✓（不加载视觉塔）
有图但视觉模型=不选 → 报错「接了参考图但没有「视觉模型」…」
无图且文生图模型为占位符 → 报错点名「文生图模型」
py_compile(nodes.py) = 0
```

---

## 本次更新 v1.7.40 —— 提示词优化节点：反推改成显式开关

以前「反推」是**猜**出来的：`模式=自动` 时，有图但没写用户提示词 → 当反推；写了词 → 当图生图。
想强制反推/强制不反推只能改「模式」，而「模式」早就从界面上撤掉了 → 实际没法控制。

现在节点第二行就是一个开关：

```
用户提示词   [多行文本框]
是否反推     [ ⬤ ]   ← 开 / 关
```

| 开关 | 走哪个系统提示词 | 行为 |
|---|---|---|
| **开（反推）** | `SYS_REV`（反推专用系统提示词） | 按「系统提示词 + 用户提示词」把参考图里的内容反推成一条可复现提示词。用户提示词会作为“要抓的重点”一起发过去（留空=整图反推）。**必须接一张参考图**，没接会直接中文报错 |
| **关（不反推）** | 有图 → `SYS_I2I`；无图 → `SYS_T2I`（系统优化提示词） | 用内置系统优化提示词改写/写提示词，不再猜模式 |

细节：
- 开关状态会原样输出到第三路 `是否反推`（BOOLEAN），后面的节点可以据此分流。
- 反推时仍然可以勾「透明背景」，会在指令里追加“背景要求透明”。
- 开了反推但没图 → `ValueError: 开了「是否反推」但没有接参考图…`（不再静默跑成图生图）。
- 老工作流的 `模式` 字段不再参与判断（它已经不在界面上），行为一律以这个开关为准。

### 本版实测（伪造 rpc，检查实际发出去的 system / user 文本）

```
widget 顺序: 用户提示词 / 是否反推 / 使用远程模型 / 服务端地址 …（与函数签名一致）
开 + 有图:  system 含反推规则 ✓  user 带进用户提示词 ✓  输出 是否反推=True
关 + 有图:  system 换成图生图系统提示词（不含反推）✓  输出 False
关 + 无图:  system 换成文生图系统提示词 ✓
开 + 无图:  报错「开了「是否反推」但没有接参考图：反推必须有一张图。」
py_compile(nodes.py) = 0
```

---

## 本次更新 v1.7.39 —— 加载时直接告诉你「这个模型塞不塞得进显存」

现象：12GB 卡加载一个 16GB 的 Qwen3-VL 8B 文本编码器 → 专用显存 11.5/12GB、**共享显存 19.9GB**、
GPU 利用率 99% 但温度只有 37℃、速度慢一个数量级。这不是显存泄漏，是**权重溢出到系统内存**：
Windows 的 WDDM 允许显卡直接寻址内存，ComfyUI 把放不下的那份留在共享显存里，每次前向都从内存
经 PCIe 搬权重 —— 带宽瓶颈，算力根本没用上，所以利用率 99% 却不发热。

**这一版**：`get_clip` 加载完成后按「权重文件总体积 vs 卡容量/当前空闲」算一次，超了就在日志里直说：

```
[服务端] 警告：CLIP「qwen3vl_8b_fp16」权重约 16.1GB，这张卡 12.0GB / 当前空闲 11.2GB —— 装不下，
会被 Windows 拆到「共享显存」(系统内存) 里，每次前向都要走 PCIe。
表现就是 GPU 利用率 99%、温度却不高、速度差一个数量级。建议换 fp8/int8 版（约一半体积）或换更大的卡。
```
塞得下就报一句正常的：
```
[服务端] CLIP「qwen2.5vl_7b_fp8」权重约 8.3GB，显存 12.0GB（空闲 11.2GB），可以整份放进去
```

### 实测

```
py_compile(backend.py) = 0
逻辑：16.1GB > 12.0GB×0.92 或 > 空闲×1.02 → 警告；8.3GB 对 12.0GB → 提示「可以整份放进去」
```

---

## 本次更新 v1.7.38 —— 参考图只算一次：负向提示词复用正向的视觉编码

**背景**：图文编码节点对「正向提示词」「负向提示词」各发一次 `clip_encode` RPC，两次带的是**同一批图**。
以前服务端老老实实算两遍：视觉塔（Qwen-VL visual，最贵，几十秒级）+ VAE reference latent 都重跑。
两张 1MP 图时，视觉塔要跑 4 遍，这就是「接两张图突然很慢」的主要来源。

**现在**：

1. **视觉塔缓存**：给 CLIP 的 `preprocess_embed` 挂一层包装，key = `blake2b(图片像素)+形状`，
   同一张图（同模型、同缩放尺寸）只算一次；第二次直接从缓存拿 `merged` + `deepstack`，搬回设备继续用。
   缓存的是 CPU 副本（每张 1MP 约 20–90MB，取决于 deepstack 层数），最多留 4 张，LRU 淘汰。
2. **reference latent 缓存**：`(vae 模型名, 图片指纹)` → latent，同样只编一次；`unload` 时清空。
3. **耗时日志**：每次编码都打三段时间，以后慢一眼看出来是卡在哪：
   ```
   [服务端] VAE 编码 reference latent：0.42s
   [服务端] 视觉塔复用命中（55dc1341…），这张图不再重算 vision
   [服务端] 耗时：tokenize 0.03s + 编码 12.60s（2 张图 / 1012 vision token）
   ```
4. 想关掉：`config.json` 里 `"vision_cache": false`（默认开）。

**预期效果**：正负两条条件里，第二条 RPC ≈ 只剩文本编码 + 拼接，视觉塔那几十秒被省掉，
整体接近**减半**（图越多、越大，省得越多）。出图结果不变 —— 缓存复用就是同一份视觉特征。

### 实测（本机 RTX 5070，构造 560×560 伪图片验证缓存行为）

```
视觉塔调用次数：同图两次 + 另一张图一次 → 2 次（正确，同图没重复算）
同图两次输出完全一致：True（merged 与 deepstack 全部相等）
不同图结果不同：True（不会串图）
grid 复用：True
latent 缓存命中：True，换 vae 名不串：True
py_compile(backend.py) = 0
```

> 备注：缓存只跨「同一次工作流里的正/负两条」，不做长期缓存；换图/换模型/换 MP 都会自动 miss。

---

## 本次更新 v1.7.37 —— 「清空显存 / 卸载模型」点了没反应：现在真卸干净

**为什么会"没用"**（两条都是真原因）：

1. **只卸了 CLIP/VAE，没动 GGUF**。你机器上 8G 显存里 7.4G 是 llama_cpp 的 GGUF（31 层 offload 到显卡），
   它跑在**独立的 worker 子进程**里；旧按钮调的是 `/rpc {"op":"unload"}`，只清服务端进程内的 CLIP/VAE 缓存。
   GGUF 一直占着 → 看显存几乎没变，像是按钮失灵。
2. **服务端不是面板拉起来的**（你自己用 `.bat`/另开窗口启动），面板里 `proc` 是空的 → 旧代码直接弹
   「服务没开，没有需要释放的显存」，连请求都没发。

**现在的行为**：

- 点一次 = 三件事按顺序做：
  1. `rpc unload` → 卸载 CLIP / VAE，并 `unload_all_models` + `soft_empty_cache` + `torch.cuda.empty_cache()`；
  2. `rpc llm_unload` → 让 llama worker 退出进程（**GGUF 那 7G 显存这时才真还回去**）；
  3. 再拉一次 `/health?light=1` 刷新面板状态。
- 判断"服务在不在"改成**探测 `/health`**，不再依赖面板自己启动的进程；服务端是外部启动的也能卸。
- 日志里给数字，一眼看出动了没有：
  ```
  [ok] CLIP/VAE 已卸载：clip:qwen_image_clip.safetensors
  [ok] 显存 7.85GB/7.98GB → 0.42GB/7.98GB
  [ok] GGUF / llama worker 已退出（这部分显存是真还回去了）
  ```
- 服务端正忙着算（`self._lock` 被占）时，60 秒后不再装没事，而是明确说：
  「CLIP/VAE 那一侧还在算别的东西，等当前任务跑完再点一次」。
- 网页面板按钮 `/api/unload` 同步改成「卸 CLIP/VAE + 卸 GGUF」，结果进日志。

### 实测（本机 RTX 5070，未加载模型 / 未起 worker 场景是否都稳）

```
Backend.unload()   → freed=[]（缓存本来空），vram 1.14GB→1.14GB，无异常
Backend.llm.unload() → 状态 未加载，worker 退出流程正常
GUI.clear_vram()   → GET /health?light=1 → unload(60s) → llm_unload(180s) → GET /health?light=1
日志：CLIP/VAE 已卸载 / 显存 7.85→0.42 / GGUF worker 已退出 / 状态已刷新
py_compile(gui.py, panel.py) = 0；新 exe --smoke 退出码 0（11,568,855 B）
```

> 提醒：GGUF 卸载后再用「提示词优化」会重新加载一次（AMD 那台实测 `load_seconds≈3.6`，不算慢）；
> 只想释放显存又不想反复加载，就只停「生成」别点这个按钮。

---

## 本次更新 v1.7.36 —— 修掉「vae 与 clip 指向不同服务端」这个硬报错

报错原文：

```
ValueError: 「vae」与「clip」指向不同服务端，请用同一台服务端的 CLIP / VAE 加载器
```

**为什么会报**：图文编码节点真正干活的只有 **CLIP 那一台机器**，VAE 只是把「模型名」丢给那台机器去加载，
两边地址根本不需要字符串完全相等。v1.7.35 之前用的是 `rstrip("/")` 字面比较，所以下面这些**同一台机器**也被判成"不同"：

| 会被误判的写法 | 举例 |
|---|---|
| 一边带 `http://` 一边不带 | `http://111.180.124.155:8199` vs `111.180.124.155:8199` |
| 末尾多一个 `/` | `http://x:8199/` vs `http://x:8199` |
| 前后有空格 / 大小写不同 | ` HTTP://x:8199 ` |
| VAE 加载器的地址还留着 `<未填>` 占位 | `<请填服务端地址>` |

**现在怎么处理**：
1. 地址先规范化（去 `http(s)://`、去末尾 `/`、去空格、转小写、去 `:80/:443`）再比；只是格式不同 → 直接放行。
2. VAE 加载器地址是空 / `<…>` / `（…）` 占位 → 自动沿用 CLIP 的服务端，不再报错。
3. 真是指向另一台机器 → **不拦了**，只在日志里提示一行，然后照 CLIP 那台去加载同名 vae：
   ```
   [远程CLIP图文编码] 注意：「远程 VAE 加载器」填的是 http://127.0.0.1:8199，CLIP 用的是 http://111.180.124.155:8199；
   本次仍按 CLIP 的服务端加载 vae=「qwen_image_vae.safetensors」。若那边没有这个 vae，把 VAE 加载器的地址改成同一个（或留空）即可。
   ```
   那台机器扫不到这个 vae 时，服务端会明确报「找不到 vae 模型 xxx」，比现在这个含糊的 ValueError 好定位。

### 你这边怎么弄（任选一步就能跑）

1. 把「远程 VAE 加载器」的 **服务端地址** 改成和 CLIP 完全相同（推荐，最省事）；
2. 或者那个 VAE 加载器的地址 **直接留空**（VAE 会自动跟着 CLIP 走）；
3. 更新到 v1.7.36 后，即使两边写法不一样也不会再被这个 ValueError 卡住。

> 提醒：VAE 模型必须能在 **CLIP 那台机器的模型目录** 里被扫到（同名），图文编码是那台机器算完再回传条件的。

### 本版实测

```
_norm_url 规范化比较:  http://111.180.124.155:8199/ == 111.180.124.155:8199  → True
                       " HTTP://111.180.124.155:8199 " == http://111.180.124.155:8199 → True
                       http://111.180.124.155:8199 != http://127.0.0.1:8199 → False（真不同，只提示不报错）
模拟 clip=111.180.124.155:8199 / vae=127.0.0.1:8199 → 打印提示后照常发起请求（不再 ValueError）
py_compile(nodes.py) = 0
```

---

## 本次更新 v1.7.35 —— 「远程 CLIP 图文编码」的参考图尺寸也改成百万像素（MP）

和「提示词优化」统一：原来那栏是 `参考图像素`（INT，默认 1048576 这种绝对像素数，不好读），
现在改成一模一样的那一套：

```
参考图最大像素   [ −  1.00  + ]    ← 单位：百万像素（MP）
```

- 默认 **1.00 MP**（≈1000×1000），只缩不放、宽高对齐 32；`0` = 保持原尺寸
- 换算：`总像素 = MP × 1,000,000` → 服务端 `resolution = √总像素`，再按 32 对齐缩放
  （1.00 → 1000²，0.4 → 632²≈400K，2.0 → 1414²≈2M）
- 这栏同时决定**文本编码器 vision 槽**和 **VAE reference_latents** 的缩放（还是官方那套保证 2×2 slot）
- **老工作流兼容**：残留的 `参考图像素`（1048576 这种像素数）仍然会被读，数值不走样
- 日志里会打：`[远程CLIP图文编码] 图 2 张 | 参考图最大像素=1MP（1000000 px）| vae=xxx`

### 本版实测

```
RemoteCLIPImageEncode required: … / 参考图最大像素 / 卸载模型
参考图最大像素 opts: {"default": 1.0, "min": 0.0, "max": 32.0, "step": 0.05}
py_compile(nodes.py) = 0
```
---

## 本次更新 v1.7.34 —— 底层超时限制全部取消（客户端 + 服务端 + LLM 子进程三层都没有上限）


上一版只是把「超时时间」这一栏从节点上删掉了，**底层还在按老值砍**（CLIP/VAE 120s、600s，
提示词优化 160s 一路传到服务端 LLM 子进程）。这一版把三层全改成"无限等"：

| 位置 | 以前 | 现在 |
|---|---|---|
| `remote_api.rpc` / `health` / `unload` / `llm_unload` | 120 ~ 600s | `timeout=None`（阻塞直到服务端回话） |
| `RemoteCLIP.timeout` / `RemoteVAE.timeout` | 120 / 600 | `None` |
| 提示词优化 → 服务端 `llm_chat` | 节点 160s + rpc 280s | 两层都 `None`，`params.timeout=None` |
| 服务端 `llm.py::_call` | `Event.wait(600)` 抛"LLM 子进程超时" | `Event.wait(None)`，一直等 worker 回 |
| 服务端 `llm.py::chat` / `backend.py` llm_chat | `timeout=600` | `None` |
| 外部 llama-server 客户端 `LLMServerClient` | 600 | `None` |

于是：大 CLIP 首次加载、超大图 VAE 解码、AMD 那台机视觉编码 2 分半、LLM 慢慢吐 2000 token，
都**不会中途被砍**。要停就手动在 ComfyUI 里点「取消」。

> 代价说明：如果服务端真的卡死/断网，节点会一直等（不再自己报"超时"）。取消队列即可。
> 只保留两个例外（都是秒级健康探测，不影响出图）：启动时枚举模型清单 `list_models` 4s、
> 前端「🔄 刷新模型列表」的 probe 5s。

### 本版实测

```
rpc.timeout default    = None
health.timeout default = None
unload / llm_unload    = None
RemoteCLIP.timeout attr = None
RemoteVAE.timeout attr  = None
optimizer 已无固定超时 ✓
VAE 解码 已无固定超时 ✓
py_compile(nodes.py / remote_api.py / llm.py / backend.py) = 0；exe --smoke = OK
```
---

## 本次更新 v1.7.33 —— 所有节点的「超时时间」删掉；「种子值 + 种子模式」合成原生 style 的一个控件

### 1）「超时时间」整栏删除（7 个节点全删）

| 节点 | 内部固定超时 |
|---|---|
| 远程 CLIP 加载器 / 远程 VAE 加载器 | 5s（连服务端的 health 探测） |
| 远程 CLIP 文本编码 / 远程 CLIP 图文编码 | 120s |
| 远程 VAE 解码 / 远程 VAE 编码 | 600s |
| 提示词优化 | `DEFAULT_TIMEOUT`（160s） |

时间都够用（服务端首次加载大 CLIP 也在这之内）；老工作流里残留的 `超时时间` 值仍然会被读（`kw.get("超时时间")`），
所以旧图不会因为少一栏而报错。要改默认值就改代码里的常量，不用再占节点一行。

### 2）「种子值」+「种子模式」合成一个控件（ComfyUI 原生 seed style）

以前是两栏：`种子值`（INT）+ `种子模式`（随机 / 固定）。现在只留一栏，跟图二那种一模一样：

```
种子值  [ − 0 🎲 + ]   ← 右侧那个蓝色按钮点开 = 固定值 / 递增值 / 递减值 / 随机值
```

实现就是 ComfyUI 自己的 `"control_after_generate": True`（和 KSampler 的 `seed` 同一套前端逻辑）：
选「随机值」→ 每次排队后自动换种子；「固定值」→ 一直用这个数；「递增值 / 递减值」→ 每次 +1 / -1。

### 3）本版实测（`INPUT_TYPES()` 实测，7 个节点全过）

```
RemoteCLIPLoader      required: 服务端显卡 / 计算设备 / 服务端地址 / 模型 / 模型类型 / 卸载模型
RemoteVAELoader       required: 服务端显卡 / 计算设备 / 服务端地址 / 模型 / 卸载模型
RemoteCLIPTextEncode  required: 正向 / 负向 / 服务端显卡 / 计算设备 / 服务端地址 / 模型 / 模型类型 / 卸载模型
RemoteCLIPImageEncode required: 同上 + 参考图最大像素(MP)
RemotePromptOptimizer required: 用户提示词 / 使用远程模型 / 服务端地址 / API Key / 文生图模型 / 图生图模型 /
                               视觉模型 / 参考图最大像素 / 输出语言 / 透明背景 / 种子值 / 卸载模型
RemoteVAEDecode       required: vae / latent / 卸载模型        optional: 分块解码 / 分块尺寸 / 重叠 / 输出设备
RemoteVAEEncode       required: vae / 图像 / 卸载模型          optional: 分块编码 / 分块尺寸 / 重叠

种子值 opts: {"default": 0, "min": 0, "max": 4294967295, "control_after_generate": true, ...}
ALL OK
```

> 字段又少了两栏（`超时时间`、`种子模式`），老工作流的控件值还是会错位——照样建议把节点重新拖一遍。
---

## 本次更新 v1.7.32 —— 「手动模型名」全部删掉；`device` 不再手选，改成自动识别远端在算的是显卡还是 CPU；显卡栏只留型号

### 1）所有节点的「手动模型名」删掉

模型只有一个来源：点 **「🔄 刷新模型列表」** 后，「模型」下拉里的条目。文本编码 / 图文编码节点上那栏也删了
（加载器在 v1.7.24 已经删过）。老工作流里残留的 `手动模型名` 值**一律忽略**，不会报错、也不会被采用。

### 2）`device`（auto / cpu / gpu）改成「计算设备」只读栏，自动识别

以前两个编码节点末尾有个 `device` 下拉让你选条件张量放哪。现在：

| 以前 | 现在 |
|---|---|
| `device`：`auto` / `cpu` / `gpu` 手选 | `计算设备`：只读，点刷新自动从服务端读回，值只有 **显卡** / **CPU** |

判定依据是服务端 `/health` 里的 `device` + `device_name`：`cpu` 或压根没报到显卡型号 → **CPU**，否则 → **显卡**。
条件张量一律跟原生节点一致（本机模型设备），不用再选。

### 3）「服务端显卡」只显示型号

原来显示 `NVIDIA GeForce RTX 4070 12.0GB · CUDA`（型号 + 显存 + 计算方式）。现在只留型号：

- 有显卡：`AMD Radeon RX 5700 XT`（就这一串，不带 · CUDA / · ROCm，也不带显存）
- 纯 CPU 机器：`（无显卡，纯 CPU 计算）`
- 连不上：`（连不上服务端）`

### 4）本版实测（服务端 111.180.124.155）

```
probe clip -> {'ok': True, 'device': 'AMD Radeon RX 5700 XT', 'compute': '显卡'}  models=4
probe vae  -> {'ok': True, 'device': 'AMD Radeon RX 5700 XT', 'compute': '显卡'}  models=5
probe llm  -> {'ok': True, 'device': 'AMD Radeon RX 5700 XT', 'compute': '显卡'}  models=2
```

节点字段（实测 `INPUT_TYPES()`）：

```
RemoteCLIPLoader       required: 服务端显卡 / 计算设备 / 服务端地址 / 模型 / 超时时间 / 模型类型 / 卸载模型
RemoteVAELoader        required: 服务端显卡 / 计算设备 / 服务端地址 / 模型 / 超时时间 / 卸载模型
RemoteCLIPTextEncode   required: 正向提示词 / 负向提示词 / 服务端显卡 / 计算设备 / 服务端地址 / 模型 / 超时时间 / 模型类型 / 卸载模型
                       optional: clip
RemoteCLIPImageEncode  required: … / 参考图最大像素(MP) / 卸载模型   optional: clip / vae / 图片1…图片10
```

> 字段变多了（多出「计算设备」），老工作流的控件值会错位，**建议重新拖一个节点出来**，
> 或者把节点上这几个下拉重新选一遍再保存。
---


## 本次更新 v1.7.31 —— 模型只填「模型根目录」：按文件夹名自动归类 vae / clip / clip_vision / LLM；面板直接告诉你「谁在 CPU 里算」

### 1）配置区只剩一行：模型根目录（可加多个）

以前要分别填「CLIP 模型目录」「VAE 模型目录」「GGUF 模型目录」，填错一个节点里就空一片。现在改成：

```
┌ 模型根目录 ────────────────────────────────────┐
│ F:\Comfy-Desktop\ComfyUI-Shared\models        │  ← 可以加多行
│ D:\models2                                     │
└────────────────────────────────────────────────┘
      [添加目录…] [移除选中] [自动找]
```

规则（只按**文件夹名**判断，不看文件名）：

| 文件夹名叫 | 归到 |
|---|---|
| `vae` | VAE |
| `clip` / `text_encoder` / `text_encoders` | 文本编码器（CLIP） |
| `clip_vision` / `clipvision` | 视觉编码器（clip_vision） |
| `llm` / `gguf` | 提示词优化的 GGUF；文件名带 `mmproj` / `vision` / `vit` 的自动进「视觉投影」 |
| 其它（`loras`、`checkpoints`、`unet`、`diffusion_models`…） | 整棵跳过，不扫 |

- 一个根目录可以加多个；扫到同名文件会在名字后加 `_2`
- `diffusers<xxx>/vae/diffusion_pytorch_model.safetensors` 这种也算 VAE，条目名会自动带上父目录名（`vae_diffusion_pytorch_model`），不会出现一堆 `model`
- 填了根目录后，老的 `clip_model_dir` / `vae_model_dir` / `llm_model_dir` 不用再填；面板保存时会把它们从配置里删掉
- 服务端启动 / `/reload` 时会重新扫一遍，**加了模型不用重启**，点一下「扫描并加入模型」或「重读配置」就行
- 原来手写的 `models.clip/vae` 条目保留，扫描只会补没登记过的文件

配置写法（config.json，等价的两种）：

```json
"model_roots": ["F:/Comfy-Desktop/ComfyUI-Shared/models"]
```

```json
"model_roots": [
  "F:/Comfy-Desktop/ComfyUI-Shared/models",
  "D:/ComfyUI/models"
]
```

### 2）状态栏会说「多少张量还在内存里算」

加载完 GGUF 后，服务端从 ggml 的加载日志里把 `Vulkan0 compute buffer size` 和 `CPU compute buffer size` 抓出来，状态行多一段：

```
…… 上卡: offloading 31 repeating layers to GPU    缓冲: 显存 5.6G / 内存 4.4G    ⚠ 模型主体在显卡，但还有 4.4 GB 张量在内存里算（显存缓冲 0.3 GB）。带图做提示词优化时，视觉投影（mmproj）多半也在 CPU，这就是任务管理器里 GPU 利用率很低的原因。想快：① 优化时不带图（只用文生图 GGUF）；② 换 f16 版 mmproj（bf16 在 Vulkan 上容易回落 CPU）；③ 图先缩到 1024 以内再喂
```

### 3）你截图那台 AMD 机的结论（RX 5700 XT + Vulkan）

- `offloading 31 repeating layers to GPU` + 专用显存 7.4/8.0 GB → **模型权重确实在显卡上**，不是 CPU 版在跑
- `GPU 利用率 1%` + `CPU compute buffer size = 4484.10 MiB` + 日志里的 `encoding image 1/1 to input buffer (nx=1408, ny=1920)` → **慢在视觉编码**：1440×1920 的原图先在 CPU 里编码，`mmproj-bf16.gguf` 在 Vulkan 后端（RDNA1 没有 cooperative matrix）上几乎不参与算
- `flash attention is disabled` 是 Vulkan 后端的正常现象，不是配置错

### 4）抓到你那份 worker 日志后的定位（2026-11-22，带图那次实测）

你那次的日志里这几行是决定性的：

```
mtmd_backends: using Vulkan0 backend
warmup: WARNING: the mmproj graph uses unsupported operators by the backend
warmup:          the performance will be suboptimal
warmup:          list of unsupported ops (backend=Vulkan0):
warmup:          MUL_MAT: type = f32, ne = [8464 8464 16 1]
warmup:         SOFT_MAX: type = f32, ne = [8464 8464 16 1]
mtmd_context:    Vulkan0 compute buffer size =   285.29 MiB
mtmd_context:        CPU compute buffer size =  4484.10 MiB
encode: copying image 1/1 to input buffer (nx=1408, ny=1920)
image slice encoded in 148705 ms          ← 2 分 28 秒，全在 CPU
prompt eval time = 154292.50 ms / 2797 tokens (18.13 tokens per second)
        eval time =   6911.13 ms /  322 runs  (46.59 tokens per second)
```

**结论（这次不是猜的）**：

- 语言模型 31 层全在 Vulkan 卡上，生成 46.6 tok/s —— 这部分没问题
- 慢的是**视觉编码器**：mmproj 的注意力算子（`MUL_MAT f32 8464×8464×16`、`SOFT_MAX` 同形）在 Vulkan 后端**没有实现**，llama.cpp 只能把整张图的计算退回 CPU。CPU compute buffer 给到 4484 MiB 就是这块
- 一张 1408×1920 的原图 = **2640 个视觉 token**，f32 注意力对 token 数是平方级，于是单张图 148 秒。任务管理器里 GPU 利用率 1% 就是这么来的（那两分半卡在 CPU 上，卡基本闲着）

**这版的处理：缩图放到节点上，按「百万像素」调**

「提示词优化」节点原来没有这个设置，现在在参数区加了一项 **`参考图最大像素`（单位 MP，默认 0.4）**：

- 接进来的参考图在**本机节点这一侧**先等比缩小（只缩不放，宽高对齐 32，bicubic），再编码成 PNG 发给服务端 —— 服务端不再二次缩图（`llm_image_max_side` 保留但默认 0）
- 控制台会打一行：`[参考图] 1408×1920（2.70MP）→ 544×736（0.40MP）`，然后是 `[远程提示词优化] … 图 1 张（上限 0.4MP）`
- `0` = 不缩（原图直发）；觉得反推细节掉了就调到 `0.8 ~ 1.0`
- 实测尺寸对照（1408×1920 原图 = 2.70MP = 2640 视觉 token）：

| 参考图最大像素 | 实际发给模型的尺寸 | 视觉 token | 视觉编码耗时（该 AMD 机估算） |
|---|---|---|---|
| 0（不缩） | 1408×1920 | 2640 | 148 秒（实测） |
| 0.2 | 384×512 | 192 | 约 1 秒 |
| **0.4（默认）** | **544×736** | **391** | **约 2~3 秒** |
| 0.8 | 768×1056 | 792 | 约 10 秒 |
| 1.0 | 864×1152 | 972 | 约 18 秒 |

原因还是上面那段：mmproj 的 f32 注意力在 Vulkan 后端没有实现，会整段退回 CPU，耗时随 token 数平方涨；所以缩图是这台机唯一便宜的解法。


三条能立刻见效的做法（按性价比排）：

1. **节点上「参考图最大像素」保持 0.4**（本版默认，最稳）
2. **优化时不带图**：只挂「文生图模型」（`Qwen-Image-2.1-PE-T2I.Q5_K_S.gguf`），图那一路不接 → 预填量从 2797 tokens 掉到几百
3. **换 f16 版 mmproj**（`...mmproj-f16.gguf`）替掉 bf16：f32/bf16 混算在 Vulkan 上同样会回落，f16 通常少踩一点

> RX 5700 XT 是 gfx1010，AMD 官方 Windows 轮子只覆盖 gfx1030+，装 HIP 版通常起不来；这台就走 Vulkan。真正的根治要等 llama.cpp 给 Vulkan 补上 f32 大矩阵乘（日志里它让你去报 issue，就是这个意思）。

### 5）本版实测（本机）

```
[服务端] 模型根目录 1 个 → 自动登记 CLIP 3、VAE 6、clip_vision 0、GGUF 4、mmproj 2
list_models: clip 3 / vae 6 / clip_vision 0 / llm 1 / llm_vision 1
/reload → model_roots 1 个、scanned 9
_parse_buffers("Vulkan0 … 285.29 MiB / CPU … 4484.10 MiB") → (285.29, 4484.1)
节点缩图（fit_max_pixels，1408×1920 原图 2.70MP）：0.2→384×512(192tok)、0.4→544×736(391tok)、0.8→768×1056(792tok)、1.0→864×1152(972tok)，0=原样 2640tok
```

（本机 `D:\Comfy-Desktop\ComfyUI\ComfyUI\models` 下 GGUF 在 `LLM\GGUF\Qwen\<型号>\*.gguf`，是 4 层深，所以扫描深度从 3 放到 5，否则正好扫不到——你 F: 那几个目录也是这个结构。）

---

## 上一版 v1.7.30 —— 「到底是谁在算」说清楚：环境诊断 + 一键卸载 + 真跑分量速

你服务端那台 AMD 机我直接连上去测了，先把**事实**摆出来（`/llm_status` 原样）：

```
backend=vulkan   llama_cpp=0.3.37   offloaded="offloading 31 repeating layers to GPU"
device_name=AMD Radeon RX 5700 XT
短提示：prompt eval 84.9 tok/s，生成 48.0 tok/s
你那次真实请求（带 1 张图，2797 个 token）：prompt eval 18.07 tok/s → 一次 175 秒
```

也就是说：**生成确实在显卡上跑**（48 tok/s，纯 CPU 做不到这个数），
**卡住你的是"预填"那一段**——大段提示 + 图片编码在 Vulkan 后端上掉到 18 tok/s，
175 秒里绝大部分时间花在这。RX 5700 XT（gfx1010）没有 cooperative matrix，
Vulkan 的大 batch 矩阵乘只能走慢路径，这是卡本身的限制，不是没上卡。

这一版把"判断"交到你手里，三件事：

### 1）「环境诊断（装了几版 llama）」

点一下就把这个 ComfyUI 的 python 里的真实情况打出来：

- import 到的到底是哪份 `llama_cpp`（绝对路径 + 版本）
- pip 里**登记了几份** llama-cpp-python（>=2 份就是有重复安装）
- `llama_cpp\lib` 里到底带了哪些原生库 —— 有 `ggml-vulkan.dll`/`ggml-cuda.dll` 才是 GPU 版，
  只有 `ggml-cpu*.dll` 就是 **CPU 版**
- site-packages 里有没有**第二个 llama_cpp 目录盖在上面**（源码编译装过最容易留这个，导入的可能是那份 CPU 版）
- `C:\Windows\System32\vulkan-1.dll` 在不在（缺了它，Vulkan 版会静默回落到 CPU）
- torch 自带的 GPU 库、以及有没有人设过 `GGML_VK_VISIBLE_DEVICES` 之类环境变量

结论会直接写成一句人话：`CUDA 版（走显卡）` / `vulkan 版（走显卡）` / `CPU 版（只在内存里算）`。

### 2）「卸载 llama-cpp-python」

你之前装过 CPU 版，最怕的就是"两份并存、导入到旧那份"。这个按钮做两遍：

1. `pip uninstall -y llama-cpp-python`（再补一刀 `llama_cpp_python`）
2. 再扫 site-packages，把 pip 删不掉的**残留目录**（`llama_cpp\`、`llama-cpp*`、`*.dist-info`）
   统统删掉，删完再 import 一次确认真没了

会先停掉服务、杀掉 LLM worker（否则 `.pyd` 被占用删不掉）。**卸载完必须再点
「装 Vulkan 版（AMD·免SDK）」把带 GPU 后端的版本装回来**，否则 GGUF 加载不了。

> 实测（本机临时 venv）：先正常卸掉 `0.4.7+cu130` → `removed=[]`、`still_importable=[]`；
> 再手工造一个假的 `llama_cpp` 残留目录 → 一键卸完自动 `rmtree` 掉，`still_importable=[]`。

### 3）「GPU 跑分（预填/生成）」

让服务端真跑一次（512 token 预填 + 64 token 生成），读 llama.cpp 自己的
`llama_perf_context_print` 行，给出两个数：

- **PP（预填）tokens/s**：长提示、带图时主要看这个
- **生成 tokens/s**：一般 30+ 就是显卡在算；低于 10 基本就是 CPU 版

本机 RTX 5070 实测（同一个 `Qwen-Image-2.1-PE-T2I.Q5_K_S.gguf`）：
`PP 2484.6 tok/s · 生成 85.8 tok/s · offloading 31 repeating layers to GPU → 结论：显卡在算（生成速度正常）`。

### 你现在怎么做（针对"计算还在 CPU"）

1. 面板点「环境诊断」。
   - 如果写 `CPU 版` 或提示有 **残留 llama_cpp 目录 / 多份登记** → 点「卸载 llama-cpp-python」，
     再点「装 Vulkan 版（AMD·免SDK）」，完了点「加载 GGUF 到显存」+「GPU 跑分」复核。
   - 如果写 `vulkan 版（走显卡）` 且跑分生成 ≥30 tok/s → **其实已经在卡上算了**；
     慢在预填（尤其带图）。这时能提速的只有换后端：点「装 HIP 版（AMD·免SDK）」
     （走 AMD 官方预编译 wheel，同样不用 SDK），装完再跑一次分对比。
2. 想确认"有没有真的用显卡"：**别看任务管理器**——Windows 任务管理器的 GPU 占用统计
   对 Vulkan/ROCm 计算不可靠；只看「GPU 跑分」的两个数字和 `上卡: offloading N layers to GPU`。

> 路线不变：GGUF 还是跑在服务端那个 ComfyUI 的 python 里、和 CLIP/VAE 共用 8199 一个端口，
> 不另开 llama 端口；卸载/重装只是换 `llama_cpp` 的构建（cpu / vulkan / hip），不改这套架构。

---

## 上一版 v1.7.29 —— 修你实机上的两个故障：状态栏一直是「—」、GGUF 扫不到

你截的两张图我都能对上原因，都是**服务端这一侧**的，不是你装错、也不是 AMD 卡的问题：

### 故障一：面板「运行状态」全显示 —（不更新）

面板每 3 秒 GET 一次 `/health`，超时设的是 **4 秒**。而你服务端这台机 `/health` 每次要 **4.09–4.15 秒**
（我直接从公网 `111.180.124.155:8199` 连过去测了多次，稳定这个数）——因为 `/health` 里塞了完整模型清单，
要去 `F:\Comfy-Desktop\ComfyUI-Shared\models` 这个共享盘上 stat 上千个文件。

结果就是：**每次都刚好超时 → 返回 None → `apply_health` 永远进不去 → 标签停在默认的「—」。**
服务本身一点问题没有，是"响应比超时慢 90 毫秒"。

改法：

- `app.py`：`/health` 支持 **`?light=1`**（`/`、`/status` 同）。轻量模式下 `models` 返回 `{}`，
  不再扫盘；`/models` 反而改成 `ttl=0` 强制拿最新清单。
- `backend.py`：`status(light=False)`；`list_models(ttl=4.0)` 加了 4 秒缓存，连着请求不会反复扫盘。
- `llm.py`：`LLMEngine.status(light=...)`——轻量时连 GGUF 目录都不枚举（`MockLLM` / `ExternalLLM` 同样接受 `light` 参数，不再 TypeError）。
- `gui.py`：`_http_get(path, timeout=4)` 加形参；`_poll_health` 改打 `**/health?light=1`**，超时放到 **8 秒**。

你机上实测该修好后 `/health?light=1` 只剩 **890 字节量级**（改造前 4007 字节），耗时从 4 秒级掉到毫秒级。

### 故障二：节点扫得到 CLIP/VAE，扫不到 GGUF

两个叠加的原因：

1. **你机上的 `config.json` 里还留着 v1.7.26 写进去的 `"llm_external": "http://127.0.0.1:8300/v1"`。**
   只要这一行在，`Backend` 就会走 `ExternalLLM`——它的 `models` 恒为空，而 v1.7.27 起面板已经没有
   「外部 LLM 服务地址」这个输入框了，可旧值被回填、又被 `_collect_cfg` 原样写回，于是**永久自锁**：
   GGUF 清单永远是空，插件端就提示"服务端没扫到 GGUF"。
2. 即使忽略 external，旧代码也只扫 `<comfyui_dir>/models/LLM*`。**你的模型实际在
   `F:\Comfy-Desktop\ComfyUI-Shared\models\...`（隔壁目录），不在 ComfyUI 自己的 models 下**，所以照样扫不到。

v1.7.29 三层一起清 + 自动发现共享目录：

- `backend.py`：`__init__` 直接 `pop` 掉 `llm_external`，只建 `LLMEngine`，并打印
  `[服务端] 已忽略 config.json 里残留的 llm_external …`。
- `config.py::load()`：读配置时就丢掉这一项（`[配置] 已丢弃残留的 llm_external …`）。
- `gui.py`：不再回填 `var_llm_ext`，`_collect_cfg` 无条件 `pop` —— 老配置**不用你手改**，新包自己清。
- `llm.py`：`_model_roots()` 从 ComfyUI 目录**向上两层**枚举兄弟目录，凡是含 `models` 的都算
  （`F:\Comfy-Desktop\ComfyUI-Shared` 就是这么被找到的）；`llm_dirs()` 再在每个根下找
  `LLM / llm / llm_gguf / LLM_gguf / gguf / text_encoders_llm` 里真实存在的那个，按 `normcase` 去重
  （Windows 上 `LLM` 和 `llm` 是同一个目录，不会出现两份）。面板的「GGUF 模型目录」下拉用同一套逻辑。
- 清缓存：`/reload` 会同时清掉 CLIP/VAE 与 GGUF 的清单缓存。

修好后 `F:\Comfy-Desktop\ComfyUI-Shared\models\LLM` 自动出现在「GGUF 模型目录」里，
`.gguf` 进「GGUF 模型」、`mmproj-*.gguf` 进「视觉模型（mmproj）」，不用手选。

### 你现在要做的（三步）

1. 服务端那台 AMD 机：**先点「停止服务」**，把旧的 `RemoteCLIPVAE服务端.exe` 换成新包里的同名文件
   （`config.json` 可以留着，里面的 `llm_external` 新包会自己清掉——想干净也可以直接删掉这一行）。
2. 再「启动服务」，打开面板等 3 秒：状态栏应变成 `运行中（PID …）· 端口 8199 · 已运行 Ns`，
   设备行出 `AMD Radeon RX 5700 XT`，显存条有数。
3. 看「GGUF 模型目录」是否已经指向 `F:\Comfy-Desktop\ComfyUI-Shared\models\LLM`。
   若还是空，说明你的 `.gguf` 放在别的路径 → 在那个下拉里手动选一次，点「刷新模型列表」，
   服务端会 `/reload` 重读，插件端节点下拉立刻就有。
   （浏览器直接验：`http://111.180.124.155:8199/models` 的 `llm` 里能看到 `xxx.gguf` 就说明通了。）

### 本版实测（本机 NVIDIA + 假目录复刻你的 F: 结构，ComfyUI venv python）

| 项目 | 结果 |
|---|---|
| 残留 `llm_external` 时后端类型 | `LLMEngine`（不再是 ExternalLLM） |
| `llm_dirs()` | `…\Comfy-Desktop\ComfyUI-Shared\models\LLM`（一条，无 `llm` 重复项） |
| `status(light=True)` | 0.07s，`models` 为 `{}` |
| `status()` 全量 | 0.001s 命中缓存，`llm=['Qwen3.5-9B-Q4.gguf']`、`llm_vision=['mmproj-Qwen3.5-9B-f16.gguf']` |
| HTTP `GET /health?light=1` | 0.069s / **890 B**（改造前同机 2534 B 且含全量清单） |
| HTTP `GET /models` | 0.001s / 942 B，`llm` 与 `llm_vision` 均有条目 |
| `py_compile` | 6 个文件全过；`config.example.json` JSON 合法 |

> 说明：GGUF 仍然跑在**服务端那个 ComfyUI 的 python** 里（`llm_worker.py` 子进程 import `llama_cpp`），
> 和 CLIP/VAE **共用 8199 一个端口**，不另开 llama 端口——这条路线没变。

---

## 上一版 v1.7.28 —— AMD 卡不用装任何 SDK：直接上官方预编译 Vulkan wheel

v1.7.27 那两个按钮（`装 Vulkan 版` / `装 HIP 版`）前提是"这台机能编译"——要 Vulkan SDK 或 ROCm/HIP SDK，
再加上 MSVC C++ 和 CMake。你机器上这些都没有，所以一点就秒失败。现在改成**根本不编译**：

- llama-cpp-python 官方 wheel index 除了 `cuXXX`，还有两条 AMD 通道：`whl/vulkan` 和 `whl/hip-radeon`。
  里面是 `llama_cpp_python-0.3.37-py3-none-win_amd64.whl`——**CPython 3.8+ 通用**（你那个 3.13.12 / cp313 直接用），
  包内自带 `ggml-vulkan.dll`，不需要 SDK、不需要编译器、不需要 ROCm。
- 点 `装 Vulkan 版（AMD·免SDK）` 后做的事：抓 index 页面 → 选最新 wheel → **多线路断点续传下载 42MB**
  → 装进你那个 ComfyUI 的 python → 校验 `llama_cpp\lib` 里真出现 `ggml-vulkan`，再报"后端 = vulkan"。
  四条线路依次是：GitHub 直连、`gh-proxy.com`、`ghfast.top`、`ghproxy.net`；每拉 8MB 就主动断开重连（国内长连接必断），
  下了多少存成 `.part`，**再点一次就从断点接着下**。下好的 wheel 缓存在 `%LOCALAPPDATA%\RemoteCLIPVAE\wheels`。
- `装 HIP 版（AMD·免SDK）` 同理，走 `hip-radeon` 通道。AMD 卡一般用 Vulkan 就够，HIP 只是备选。
- 两条路都只有"下载失败 **且** 本机确实装了 SDK"时才退回去现场编译；连 SDK 都没有时给明确提示 + 手轮子的直链，
  不再提 llama-server / 外部服务地址那些已下线的说法。
- `安装 llama-cpp-python`（普通那条）在 ROCm torch 的机器上也改成先走 Vulkan 预编译 wheel，不再往 AMD 机上装 cuXXX 轮子。

架构没动：GGUF 仍然由服务端那个 python 拉起 `llm_worker.py` 加载，**和 CLIP/VAE 共用 8199，不另开端口**。

| 你的情况 | 点哪个 | 前置 |
|---|---|---|
| AMD 显卡（含 RX 5700 XT / gfx1010），不想装 SDK | **装 Vulkan 版（AMD·免SDK）** | 无（联网下 42MB wheel） |
| AMD 显卡，想试 HIP 后端 | **装 HIP 版（AMD·免SDK）** | 无（同上，走 hip-radeon 通道） |
| NVIDIA | `安装 llama-cpp-python` | 无（自动匹配 cu 预编译 wheel） |

装完点 `检测计算后端`，显示 `计算后端: VULKAN（ggml-vulkan）` 就是真上卡了；
再 `加载 GGUF 到显存`（`n_gpu_layers=-1` 全部层上卡），状态行会写 `后端: vulkan`，设备名是你的 AMD 卡。

> 注意：这台是 NVIDIA 测试机，本地 5070 上验证的是"wheel 能下、能装进 cp313、能被探测成 vulkan 后端"；
> 真机 RX 5700 XT 上的出字速度要以你那边实测为准（gfx1010 走 Vulkan 通常比同一张卡的 CPU 快数倍）。

### 实测记录（本机 cp313 空 venv，就是按你服务端的环境复刻的）

```
选中：llama_cpp_python-0.3.37-py3-none-win_amd64.whl
下载 40MB → 4 条线路轮流、断了 3 次、每次都从断点续上 → 42,201,883 字节下完，zip 校验通过
pip install → llama-cpp-python 0.3.37（cp313 装 py3-none 包，没有任何 "not a supported wheel"）
后端探测：{"backend":"vulkan","libs":[...ggml-vulkan.dll...],"devices":["NVIDIA GeForce RTX 5070 (NVIDIA)"],"offload":true}
llm_worker：ping ok / load 3.3s / 生成 96 tokens ≈ 9.8 tok/s / unload ok（同时修掉了 unload 参数不匹配的老 bug）
```


## 上一版 v1.7.27 —— GGUF 就跑在服务端自己这个进程里，**不另开 llama 端口**

先说清楚架构（这也是它本来该有的样子）：GGUF **不是**另起一个服务、另占一个端口。
服务端（监听 8199 的那个进程）会用它自己那个 ComfyUI 的 python 拉起 `llm_worker.py`，
llama-cpp-python 就在里面加载 GGUF；提示词优化节点发的每个请求，都是 `POST http://<服务端IP>:8199/rpc`
（`op=llm_chat`）走过去。**和 CLIP / VAE 完全同一个端口、同一份配置、同一个 `/health`。**

所以「装完还是 CPU」跟端口无关，只跟那个 python 里的 llama-cpp-python 是 CPU 版有关。
这一版把这件事彻底改了：

- 面板去掉了「外部 LLM 服务地址 / 下载 llama-server / 启动 llama-server」这几行（不再引导你另开 8300 端口）；
  只留一条说明：**GGUF 由服务端自己的 python 加载，走同一个 8199 端口**。
- 「提示词优化 LLM」区块现在是：`加载 GGUF 到显存` / `卸载 GGUF` / `安装 llama-cpp-python` / `装本地 wheel…`
  + **`装 Vulkan 版（AMD 显卡）`** / **`装 HIP 版（AMD ROCm）`** / `检测计算后端`。
- 识别 ROCm：如果那个 python 的 torch 是 `+rocm`（`torch.version.hip` 有值），
  `安装 llama-cpp-python` 会直接告诉你「没有 HIP 预编译 wheel，cuXXX 装了也只能 CPU」并**不再乱装 cu 轮子**。
- 本地 wheel 会先核对 Python 版本：`cp314` 的 wheel 往 `cp313` 的 python 里装，现在直接拦下来提示换版本
  （你日志里那个 `llama_cpp_python-0.4.7+cu132-cp314-...whl` + `Python 3.13.12（cp313）` 就是这个坑）。

### AMD 显卡怎么让它真的上卡（v1.7.28 起：不用装 SDK）

| 你的 python | 该点哪个 | 前置 |
|---|---|---|
| 有显卡驱动，没装任何 SDK（你目前这台就是这样） | **装 Vulkan 版（AMD·免SDK）** | 无，只联网下 42MB 预编译 wheel |
| 想试 HIP 后端 | **装 HIP 版（AMD·免SDK）** | 无，走官方 `hip-radeon` 预编译 wheel |
| 上面的 wheel 下不来、但本机真装了 SDK | 同上按钮（下载失败后会自动退回现场编译） | Vulkan SDK / ROCm HIP SDK + MSVC + CMake |

（v1.7.27 里"必须装 SDK 才能编"的说法已被 v1.7.28 的预编译 wheel 取代。）

编译是**一次性**的（5~20 分钟），装完 `llama_cpp\lib` 里会出现 `ggml-hip.dll` / `ggml-vulkan.dll`；
`检测计算后端` 显示 `计算后端: HIP（ggml-hip）` 或 `VULKAN（ggml-vulkan）` 就说明成了。
之后 `加载 GGUF 到显存` → 状态行里 `后端: hip`，`n_gpu_layers=-1`（全部层上卡），提示词优化就走 8199 用显卡算。

> 状态行会同时显示 `设备`（ROCm torch 下就是 AMD 卡名）和 `后端`；如果装的还是 CPU 版，
> 会追一行 `⚠ 这个 llama-cpp-python 是 CPU 版…`，一眼能分辨。

`config.json` 里的 `llm_external` 仍然保留（想接现成的 llama-server 可以手动填），但面板不再引导，默认不填。

实测（本机 CUDA 机）：`py_env_info` 现在回 `tag/cuda/hip/devices`（本机 `cp313 / 13.0 / 空 / RTX 5070`）；
cp314 wheel 装进 cp313 python → 提前拦下并提示；`llm.py` / `gui.py` / `backend.py` 编译通过；
`RemoteCLIPVAE服务端.exe` 重打后 `--smoke` 全 OK。

## 上一版 v1.7.26 —— AMD 显卡（RX 5700 XT）终于能用显卡跑提示词优化

**为什么「安装 llama-cpp-python」装完还是 CPU**：那一步只会找 CUDA 的预编译 wheel
（JamePeng / abetlen 的 index），找不到就退到纯 CPU 版。RX 5700 XT 是 **gfx1010（Navi10 / RDNA1）**，
Windows 上没有 CUDA —— 所以**装不出带 GPU 的 llama-cpp-python**，必须用 Vulkan 或 HIP 后端从源码编一次。
v1.7.26 加了 Vulkan 编译入口和「检测计算后端」；v1.7.27 又补上 HIP 路线，并把多余的 llama-server 端口去掉了。

## 上一版 v1.7.25 —— 「🗑 卸载模型」真能还显存了（含 exe 重打）

**为什么之前点了没用**：服务端把 CLIP/VAE 对象从自己的缓存里删掉就完事了，但 ComfyUI 的
`comfy.model_management` 里还挂着同一份模型（`current_loaded_models`），PyTorch 的缓存分配器也没归还，
所以 `nvidia-smi` 照旧显示占着。**删缓存 ≠ 卸显存**。

现在服务端 `unload` 做三件事：

1. 从 `_loaded` 缓存里删掉对应条目；
2. **无条件**调 `model_management.unload_all_models()`（这才是把 ComfyUI 那份模型真正放开的地方；
   以前只有“缓存里没东西”时才调，所以正常情况永远走不到）；
3. `synchronize` + `torch.cuda.empty_cache()` + `ipc_collect()` + `soft_empty_cache(force=True)`，让分配器也归还。

**LLM（提示词优化）**：卸载不再只是让 worker `del` 模型 —— 直接杀掉 worker 进程。因为进程里的 CUDA 上下文
本身就会占一截显存（几百 MB），只 del 模型，`nvidia-smi` 里那块还在；杀进程才彻底干净，下次用会自动重启 worker。

**能看到结果**：卸载接口现在回 `vram_before` / `vram_after`，节点上的弹框会显示
`服务端显存：1.9 GB → 0.4 GB（共 12 GB）`，控制台也会打一行 `[远程卸载] … | 显存 XGB → YGB`。

> 这次改的是**服务端代码**（`backend.py` / `llm.py`），所以 `RemoteCLIPVAE服务端.exe` 已重新打包，
> 直接用新 exe 覆盖旧的即可（配置 `config.json` 不用动）。

实测（本机 RTX 5070 12GB，强制走我们新的释放路径）：
`allocated 400MB → 0MB`、`reserved 400MB → 0MB`、整卡 `used 9.22GB → 1.17GB`（剩下的 1.17GB 是桌面/其他进程）。

## 上一版 v1.7.24 —— 两个加载器删掉「手动模型名」

「远程 CLIP 加载器」和「远程 VAE 加载器」不再有 `手动模型名` 这一栏：模型只从 `模型` 下拉选（下拉由 `🔄 刷新模型列表` 从服务端拉）。

- 现在这两栏的顺序：**服务端显卡 / 服务端地址 / 模型 / 超时时间 /（CLIP 加载器多一项 模型类型）/ 卸载模型** + 整宽刷新按钮。
- 老工作流兼容：执行侧仍然接 `手动模型名`（万一有值就优先用）；只是画布上不再显示，也不再参与新节点。
- 「远程 CLIP 文本编码 / 图文编码」两个节点**仍保留** `手动模型名`（服务端列不出清单时靠它手工点名），需要一起删的话说一声。
- 服务端列不出模型时，报错文案改成：先在服务端面板选好目录并扫描 → 点「🔄 刷新模型列表」→ 在「模型」下拉里选一个。

## 上一版 v1.7.23 —— 本地模式也用「文生图模型 / 图生图模型 / 视觉模型」这三栏

之前本地模式是另外两栏「本地模型 / 本地视觉模型」，切过去等于要重新选一遍、还容易选错。现在**取消那两栏**，两边共用同一套三栏，只是候选来源跟着开关换：

| 开关 | 文生图模型 / 图生图模型 | 视觉模型 | 额外显示 |
|---|---|---|---|
| 开（远程） | 服务端 `<ComfyUI>/models/LLM` 的 GGUF | 服务端 mmproj | 服务端地址 / API Key / 🔄刷新 / 🗑卸载 |
| 关（本地） | **本机** `<ComfyUI>/models/LLM` 的 GGUF | 本机 mmproj（只有一个时自动选上） | 无（地址与按钮自动收起） |

- 切到本地时前端会打本机新路由 `GET /remote_clipvae/local_models`（扫 `models/LLM`、`models/llm`、`models/gguf` 等），把三个下拉的候选换成本机文件；按文件名自动分流：带 `t2i` 的进「文生图模型」、带 `i2i/edit/img2img` 的进「图生图模型」，其余两边都列。
- **两边各自记住自己的选择**：来回拨开关，远程选的服务端 GGUF 和本地选的本机 GGUF 互不覆盖。
- 本机 `models/LLM` 里一个 GGUF 都没有时，下拉直接显示中文提示（含扫描过的目录），不会让你白跑一次出图。
- 执行侧：无图用「文生图模型」、有图用「图生图模型」+「视觉模型」，和远程完全同一套判定（有图无词=反推 / 有图有词=图生图 / 无图=文生图）。

## 上一版 v1.7.22 —— 提示词优化：双输出口 + 本地/远程开关

**1. `json` 输出口拆成两个 STRING 口**

| 输出 | 内容 |
|---|---|
| **提示词** | 正向提示词（纯文本，不含任何 json 引号） |
| **负向提示词** | 负向提示词（模型没给就是空串，透明背景不会污染它） |
| **是否反推** | 就是节点上那个开关的当前值（开=True） |

所以现在可以直接 `提示词 → CLIP 文本编码(正向)`、`负向提示词 → CLIP 文本编码(负向)`，不用再拆 json 字符串。

**2. 新增「使用远程模型」开关**

| 开关 | 谁跑 LLM | 摸哪个目录 | 需要的栏位 |
|---|---|---|---|
| **开（远程，默认）** | 服务端 ComfyUI 的 python 拉 `llm_worker.py` 子进程 | 服务端 `<ComfyUI>/models/LLM` | 服务端地址 / API Key / 文生图模型 / 图生图模型 / 视觉模型（+ 刷新、卸载按钮） |
| **关（本地）** | 本机 ComfyUI 的 python 直接 `llama_cpp.Llama` | 本机 `<ComfyUI>/models/LLM` | 本地模型 / 本地视觉模型 |

- 开关一拨，**另一半栏位在画布上立刻收起**（不会误填、不会报「模型名还是占位符」）；工作流里存的值不会丢，拨回来还在。
- 本地模式也认绝对路径：`本地模型` 下拉里没有的，直接把 `.gguf` 全路径填进去也认（按 basename 兜底搜 `models/LLM` 等目录）。
- 本地模式接了图但没选「本地视觉模型」（mmproj）→ 直接中文报错，不会静默降级成纯文本。
- 本机没装 llama-cpp-python 时，报错会写明：在服务端面板点「安装 llama-cpp-python」，或本机 `pip install llama-cpp-python`（CUDA 版用面板里的 JamePeng wheel）。
- 本地模式的「卸载模型」四档走**本机**：卸载 = `del` + `gc` + `torch.cuda.empty_cache()`；「运行前后自动卸载」= 跑前先放掉上一个，跑完再放。
- 本地模式跑完会在控制台打 `[提示词优化-本地] model=... | mmproj=... | 图 n 张 | 文生图/图生图/反推 | seed | tok`。

## 上一版 v1.7.21 —— 每个远程节点都加「卸载模型」下拉（4 档）+ 「🗑 卸载模型」按钮

| 节点 | 卸载谁 |
|---|---|
| 远程 CLIP 加载器 | 服务端那份 CLIP |
| 远程 VAE 加载器 | 服务端那份 VAE |
| 远程 CLIP 文本编码 | 编码用的那份 CLIP（连了 clip 端口就是上游那份） |
| 远程 CLIP 图文编码 | 同上 |
| 远程 VAE 解码 / 远程 VAE 编码 | 上游「远程 VAE 加载器」那份 VAE（地址和模型从连线自动取） |
| 提示词优化 | 服务端的 GGUF（llama-cpp 子进程退出） |

四档语义（和截图一致，默认「自动」）：

| 档位 | 行为 |
|---|---|
| **自动** | 不主动卸，交给服务端按需加载 / LRU / 空闲释放 |
| **保持加载** | 明确不卸（服务端一直留着，下次调用最快） |
| **运行后自动卸载** | 这个节点跑完就把对应模型踢出服务端显存（失败也照样卸，放在 `finally` 里） |
| **运行前后自动卸载** | 跑之前先卸一次（清掉上一个占显存的东西），跑完再卸一次 |

点「🗑 卸载模型」按钮是**立刻**让服务端卸掉该节点当前选中的那个模型，弹框会回显服务端释放了哪些（`（服务端本来就没加载）` 表示本来就空）。
服务端新增 `unload{kind, model}`、CLIP/VAE 侧走 `/rpc`，前端走本机代理 `GET /remote_clipvae/unload?url=&kind=&model=`。
token 已不在节点上：这个代理路由会自动补 `default_server.json` 里的 token。

## 上一版 v1.7.20 —— 「远程 CLIP 文本编码 / 图文编码」：`clip` 端口一连，本节点的模型字段自动收起

- 「远程 CLIP 文本编码 (Remote CLIP Encode)」和「远程 CLIP 图文编码 (Remote CLIP Encode + Images)」
  左边那个 **`clip` 输入端口**连了东西，就**不再用本节点自己的模型设置**（服务端显卡 / 服务端地址 / 模型 /
  手动模型名 / 模型类型 这 5 栏会在画布上自动收起，值也不参与计算），编码完全交给连过来的那个 clip：
  - 连的是「远程 CLIP 加载器」→ 用它带的地址、模型、clip_type（本节点里填的地址/模型一律忽略）
  - 连的是本机原生 CLIP（CLIPLoader / DualCLIPLoader 等）→ 本机直接用它的 `tokenize + encode_from_tokens_scheduled`，
    图文编码节点会把 `图片1…图片10` 以 `images=` 传给它（有 vae 就 `keep_vision=False`，没 vae 就 `True`，和 Qwen-Image 节点一致）
- 拔掉连线，5 栏立刻恢复显示，回到「用本节点自己的字段」的老行为；工作流里存下的这些值不会丢。

## 上一版 v1.7.19 —— 全节点删除 `token` 与「自定义模型名」

- 5 个使用服务端的节点都去掉了 `token` 栏，改成内部自动取 `default_server.json` / 侧边栏设置里的 token（没填就是空）。
- 「提示词优化」去掉 `自定义模型名`（两个模型下拉 + 刷新按钮已够用）；`手动模型名` 保留，作为服务端扫不到模型时的兜底。

## 上一版 v1.7.18 —— 「提示词优化」节点按参考节点重排，并加 服务端地址 / API Key / 刷新模型列表

节点长相现在和你截图里的「Qwen image 2.1 提示词优化」一致，从上到下：

```
图片1 … 图片10（接一张自动多一个空接口）
用户提示词
服务端地址
API Key
🔄 刷新模型列表   ← 整宽按钮，插在 API Key 正下方
文生图模型
图生图模型
视觉模型
输出语言        自动 / 中文 / English
透明背景        ← 开关
种子值
种子模式        随机 / 固定
卸载模型        自动 / 保持加载 / 运行后自动卸载 / 运行前后自动卸载
```

- **不再有**「接口类型 / 模式 / 上下文长度 / 温度 / 最大输出长度 / 超时时间」这些栏目，默认值直接固定：
  走服务端 GGUF（`接口类型=RemoteCLIPVAE`）、`n_ctx=4096`、`温度=0.7`、`最大输出=2048`、`超时=600s`。
  老工作流里带着这些旧控件也照样能跑（有值就用，没值就用默认）。
- **有图无词 = 反推**、**有图有词 = 图生图**（用「图生图模型」+「视觉模型」）、**无图 = 文生图**（用「文生图模型」），自动判断，不用再选「模式」。
- 接了图却没选「视觉模型」（mmproj）→ 执行阶段直接中文报错拦住，不会静默丢图。
- 「卸载模型」= 服务端显存策略：`自动` 按需加载/换模型才换；`保持加载` 跑完不卸；`运行后自动卸载` / `运行前后自动卸载` 省显存但下次要重新加载（走服务端 `llm_unload`）。
- 「文生图模型」「图生图模型」两个下拉由服务端 `models/llm` 一起刷新；「视觉模型」读 `models/llm_vision`（`（不使用 / 纯文本）` + mmproj 列表）。
- 服务端扫不到模型时，用 optional 里的「自定义模型名」手填 GGUF 文件名即可覆盖（这也让 OpenAI/Ollama 老用法还有退路）。

## 上一版 v1.7.17 —— 「提示词优化」改成直接用**服务端 ComfyUI 里的 GGUF**（模型不出服务端）

v1.7.16 是把提示词丢给远端的 Ollama / LM Studio / llama.cpp server；这一版改成**服务端自己加载 GGUF**：
模型文件就放在服务端机器的 `<ComfyUI>/models/LLM`，由**服务端那份 ComfyUI 的 python**（已带 torch 和 llama-cpp-python）加载，
本机和出图电脑都不用装任何东西，也不用手动开 Ollama。CLIP / VAE / clip_vision 的老接口完全不受影响。

### 1. 节点「接口类型」现在是三选一

| 接口类型 | 谁在算 | 需要什么 |
|---|---|---|
| **RemoteCLIPVAE 服务端 (用远端 ComfyUI 里的 GGUF)** ← 默认 | 服务端 exe 拉子进程，用 `<ComfyUI>/models/LLM` 里的 GGUF | 服务端 python 里有 `llama-cpp-python`（面板一键装） |
| OpenAI 兼容（vLLM / LM Studio / llama.cpp server） | 远端 HTTP 服务 | 一个 `/v1/chat/completions` 服务 |
| Ollama 原生 | 远端 Ollama | `ollama serve` + 已 pull 的模型 |

- 「模型名」下拉框会直接从**服务端**拉 GGUF 列表（文件名带 `mmproj` / `vision` / `vit` 的自动归到「视觉模型」）
- 「视觉模型」下拉 = `（不使用 / 纯文本）` + 服务端的 mmproj 列表；选 `（不使用 / 纯文本）` 就是纯文本优化
- 模型放在别的盘 → 服务端面板「GGUF 模型目录」填路径；或 `config.json` 写 `llm_model_dir`
- 节点里改完地址 / 点刷新按钮，下拉会立刻重拉；队列校验不再拿启动时的旧列表拦人（`VALIDATE_INPUTS` 一律放行，真错误在执行阶段用中文报）

### 2. 服务端 exe 面板新增「提示词优化 LLM」

> 就是双击 `RemoteCLIPVAE服务端.exe` / `0_启动图形界面.bat` 打开的**那个窗口**（tkinter 控制面板），
> 在「服务配置」里多了 `GGUF 模型目录` 一行，在「运行状态」下面多了一个 `提示词优化 LLM（GGUF，跑在服务端这台机上）` 区块。

- **GGUF 模型目录** 输入框（默认 `<ComfyUI>/models/LLM`，也自动认 `llm / gguf / LLM_gguf / text_encoders_llm`）
- 两个列表：**GGUF 文本模型** / **视觉投影 mmproj**，选中即高亮
- 按钮：`加载 GGUF 到显存`、`卸载 GGUF`、`安装 llama-cpp-python`（对 ComfyUI 的 python 跑 pip，逐行日志）
- 上下文长度 `nctx`（默认 4096）
- **LLM 运行实时信息** 三行：
  - `状态 / 模型 / mmproj / 上下文 / handler / 加载耗时 / llama-cpp 版本 / 设备`
  - `生成中：tokens · tok/s · 秒 · 图数 · 时间｜Python｜设备`（每 8 个 token 刷新一次）
  - `错误 / 提示`

HTTP 侧多了 `GET /llm_status`（给面板和前端轮询），RPC 侧多了 `llm_models / llm_status / llm_load / llm_unload / llm_chat`。

### 3. 服务端放模型、装依赖

1. GGUF 丢进 `<ComfyUI>/models/LLM`（视觉用的 mmproj 也丢同一个目录）
2. llama-cpp-python 装在**跑服务端的那个 python** 上：
   - 启动服务端用 **`4_用ComfyUI的Python启动服务端.bat`**（推荐，ComfyUI 的 venv 一般已经装好 llama-cpp）
   - 或面板点「安装 llama-cpp-python」：会自动读这个 python 的 **cp 版本** 和 **torch 的 CUDA 版本**，
     去 `JamePeng/llama-cpp-python` 的 release 里挑**对应的预编译 wheel**（如 `cp313 + cu130 → v0.4.7-cu130-win`），
     `--no-deps` 装进去，**不走源码编译**（Windows 上编译几乎必定失败）；
     匹配不到再退回 abetlen 官方 index（同样只接受预编译 wheel，最后退 CPU 版）
   - 公司网络下不了 GitHub → 手动去
     <https://github.com/JamePeng/llama-cpp-python/releases> 下 `cp版本-cu版本-win_amd64.whl`，
     再点面板上的 **「装本地 wheel…」**
   - exe 内置的 python 装不了（onefile 里没有编译环境）
3. 面板里点「加载 GGUF 到显存」，状态变「已加载」即成功（首次 6GB 模型约 5~8 秒；RTX 5070 实测 4.45s / 7.8s）
4. 回 ComfyUI，节点「模型名」选刚加载的那个，跑一次 —— 日志里会打印 tokens / tok/s

### 4. 实测（本机 RTX 5070 12GB，真 GGUF）

| 项 | 结果 |
|---|---|
| 模型 | `local-agent-quality.gguf` 6.02GB + `mmproj-Qwen3.5-9B…BF16.gguf` 0.86GB |
| 加载 | 4.45s / 7.8s（面板按钮 / RPC 直接调），`llama-cpp-python 0.4.3+cu130` |
| 纯文本 | 3.1s，225 tok，73.77 tok/s |
| 带图反推 | 2.7s，192 tok，71.91 tok/s（正确描述成「抽象数字像素艺术」） |
| 面板链路 | `/api/llm_load` → 12s 后 `/api/state` 显示 `state=已加载`、`device_name=RTX 5070`、`pid`、`stderr` 全对 |
| ComfyUI 提交 | `/object_info` 字段顺序正确、`/prompt` → `node_errors: {}`、`/history` status success |

**注意**：这一版对模型输出加了 `response_format={"type":"json_object"}` 语法约束（不支持的版本自动去掉重试），
所以模型不会再吐「思考过程」或半截非 JSON；个别「agent 类」小模型仍会原样复述你的提示词，换个提示词优化专用的 GGUF（如 Qwen-Image-2.1-PE）效果最好。

---

## 上一版 v1.7.16 —— 新增「提示词优化 (Prompt Optimizer, Remote)」：模型跑在远端，本机零依赖

照你给的「Qwen image 2.1 提示词优化」节点做的，但**不再在本机加载 GGUF**：节点只发 HTTP，
模型在远端机器上（Ollama / llama.cpp server / LM Studio / vLLM 都行），本机不用装 llama-cpp-python，
服务端 exe 一行没改。

### 1. 节点长这样

```
                       ┌─────────────────────────────────────────────┐
图像1 ─────────────────▶  用户提示词   [多行文本框]                    │
图像2 ─────────────────▶  服务端地址   http://10.10.10.2:8080        │
…（接一张自动多一个）      接口类型     OpenAI 兼容 / Ollama 原生        │──▶ prompt（优化后的提示词）
图像10 ◀────────────────  模型名       qwen3-vl-8b                    │──▶ json（模型原始 JSON）
                          API Key      （留空不鉴权）                  │──▶ 是否反推（BOOLEAN）
                          模式         自动 / 文生图 / 图生图 / 反推
                          输出语言     自动 / 中文 / English
                          透明背景     false
                          种子值 0 / 种子模式 随机
                          温度 0.70 / 最大输出长度 1024 / 超时时间 180
                       └─────────────────────────────────────────────┘
```

- 图片接口 `图片1…图片10`，**接一张自动多一个空接口**（和「远程 CLIP 图文编码」同一套前端逻辑）
- 三个输出和原节点一致：`prompt` / `json` / `是否反推`（v1.7.40 起这个值是节点上「是否反推」开关的状态）
- 走的是 OpenAI 兼容 `POST /v1/chat/completions`（文本 + `image_url` 的 Base64 PNG），
  选 `Ollama 原生` 则走 `POST /api/chat`（`messages.images`）

### 2. 模式怎么判（和原代码一致）

| 情况 | 实际模式 |
|---|---|
| 有图 + 用户提示词为空 | 反推（看图写提示词） |
| 有图 + 有用户提示词 | 图生图（改写编辑指令） |
| 无图 | 文生图 |
| 手动选「文生图 / 图生图 / 反推」 | 按你选的来 |

`输出语言=自动` 时跟随用户提示词的语言（提示词为空/全中文 → 中文）；`透明背景` 打开时
system prompt 里追加「背景全透明、保留 alpha」，并在结果末尾补一句透明背景要求。

### 3. 远端要准备什么

只要一个能跑 Qwen3-VL / Qwen-Image-PE 之类视觉模型的服务，任选其一：

```powershell
# llama.cpp server（推荐，模型就是原来那个 GGUF + mmproj）
llama-server -m Qwen-Image-2.1-PE-I2I.gguf --mmproj mmproj.gguf --host 0.0.0.0 --port 8080
# 节点填：服务端地址 http://<服务端IP>:8080，接口类型 OpenAI 兼容，模型名留空或随意

# Ollama
ollama serve
ollama pull qwen3-vl:8b
# 节点填：http://<服务端IP>:11434，接口类型 Ollama 原生，模型名 qwen3-vl:8b

# LM Studio：Developer → Start Server（默认 1234），接口类型 OpenAI 兼容
```

GGUF 原来放本机的话，把它拷到远端那台机器就行（本节点不再读本机 `models/LLM`）。

### 4. 不认 OpenAI 兼容的老接口怎么办

节点会直接把远端返回的报错抛到 ComfyUI 控制台（`远端 LLM 返回 HTTP 4xx：<body>`），
常见原因：模型名空着、地址少写 `http://`、防火墙没开端口、服务端只开了 `/api/generate`。

### 5. 实测（本机，mock 远端 LLM 8198）

- 5 组用例全过：文生图 / 图生图+透明背景 / 反推(2 图) / Ollama 原生 / 单节点多帧（batch=2 → 2 张图）
- 断言的请求内容：`model=qwen3-vl-8b`、`seed=1234`（固定种子）、`text part=请反推这张图的提示词。`、
  `image_url` 为 `data:image/png;base64,…`、system prompt 随模式切换
- `/object_info`：required 13 项 + optional 11 项（`图片1..图片10` + `token`），共 7 个节点注册
- `py_compile` / `node --check` 通过；服务端未改，无需重打 exe

---

## 上一版 v1.7.15 —— 图文编码节点的图片上限：32 → 10

「远程 CLIP 图文编码」的图片接口上限从 32 张改成 **10 张**（`nodes.py` 的 `IMAGE_SLOTS = 10`、
`web/refresh.js` 的 `IMG_MAX = 10`），行为不变：接一张自动多一个空槽，最多到 `图片10` 就不再长。

- 旧工作流里如果接了 11 张以上的图，多出来的连线会失效，需要重接；10 张以内完全不受影响
- 服务端没改，`RemoteCLIPVAE服务端.exe` 沿用 v1.7.13/1.7.14 那个

---

## 上一版 v1.7.14 —— 新增「远程 CLIP 图文编码 (Remote CLIP Encode + Images)」

原来的「远程 CLIP 文本编码」继续保持不变（纯文本、四项输入、两路条件）。
新增一个**并列的独立节点**，专门用来「文字 + 参考图」：

**`Remote CLIP 图文编码 (Remote CLIP Encode + Images)`**

| 项 | 说明 |
|---|---|
| 正向提示词 / 负向提示词 | 和原节点一样，出 `正向条件` / `负向条件` |
| `clip`（可选） | 接「远程 CLIP 加载器」 |
| `vae`（可选） | 接「远程 VAE 加载器」；**接上就会同时编 reference_latents**（Qwen-Image-Edit 的做法） |
| 参考图最大像素 | **百万像素(MP)**，默认 1.00（≈1000×1000）；图会按它等比缩放到 32 的倍数；0 = 原尺寸 |
| `图片1 … 图片32` | **接一张自动多出一个空接口**，拔掉自动收回去，最多 32 张 |

两种用法自动区分：
- 接了 `vae` → 服务端把同一张图一边进文本编码器 vision 槽、一边编成 `reference_latents` 挂到条件上，`keep_vision=False`；
- 没接 `vae` → 只当 vision 输入，`keep_vision=True`。
- 一张图都不接 → 自动退化成纯文本编码（和原节点等价）。

对照原生节点：它等价于 `TextEncodeQwenImage21` 的「正向/负向」两路输出（但**不输出 latent**，
所以你原来用「空Latent图像 (SD3)」的做法不用改）。原生的 2.1 节点是把参考图塞进 `image_1…image_16`；
我们这个节点用的是 ComfyUI 老式动态输入 + 前端增槽（`web/refresh.js` 的 `syncImgSlots`），
老版本 ComfyUI 也能用，且「显卡只读 / 刷新模型列表 / 地址改后自动重探」全部继承。

### 实测（本机 ComfyUI 0.44.2 + mock 服务端）

- `nodes.py` `py_compile` 通过；`refresh.js` `node --check` 通过
- `/object_info/RemoteCLIPImageEncode`：required = 正向提示词 / 负向提示词 / 服务端显卡 / 服务端地址 / 模型 / 模型类型 / 参考图最大像素(MP)；
  optional = `clip` / `vae` / `token` / `device` + `图片1…图片32`（共 36 项，32 个图片槽），输出 `正向条件 / 负向条件`
- 提交 `RemoteCLIPLoader + RemoteVAELoader + 2×EmptyImage → 图片1/图片2 → 图文编码 → PreviewAny`：
  `node_errors: {}`、`status: success`，条件的 extra 里出现 **reference_latents** 和 `pooled_output`（说明 vae 那一路生效了）

---

## 上一版 v1.7.13 —— 修「本地 CLIP 出图正常、远程 CLIP 出图糊/重影」

真机现象：同一张工作流（原生 `文本编码 Qwen Image 编辑 Plus` + 参考图 + 远程 VAE），
把 CLIP 换成「加载 CLIP（本地）」图就正常，换成「远程 CLIP 加载器」就变成灰白叠影。
对比下来是**远程路径把提示词丢了**，并且 `keep_vision` 用了错的默认值。两处都修了：

### 1. 提示词被吃掉（真正的元凶）

`RemoteCLIP.tokenize()` 把文本塞在 `{"__remote_clip__": {"text": ...}}` 里，
而 `_text_of()` 只查顶层 `__remote_clip_text__` / `text` —— 于是 `encode_from_tokens_scheduled`
取到的文本是 **空字符串**。服务端收到「空提示词 + 2 张参考图」，
Qwen-Image 只能把参考 latent 硬平均着铺满整幅图 → 就是你看到的灰白、半透明、穿不上衣服。

修法：
- `tokenize()` 返回值同时带上顶层 `__remote_clip_text__`；
- `_text_of()` 增加「先读内层 payload.text」的分支；
- 实测「把图一的衣服穿到图二身上」原样送达服务端（`sent text match: True`）。

### 2. `keep_vision` 默认值不对（第二种叠影来源）

`comfy/text_encoders/qwen_image21.py` 里 tokenizer 的签名是
`tokenize_with_weights(..., keep_vision=False, ...)` —— **默认 False**。
原生「编辑 Plus」节点不传这个参数，所以本地是 False：vision token 被换成 `image_slots`，
参考信息只由节点自己挂的 `reference_latents` 提供，一份不多一份不少。

服务端 v1.7.9 起的推断是「本地没编 latent 就 keep_vision=True」，于是远程变成
**vision token 留一份 + reference_latents 再挂一份**，图像信息插了两次 → 叠影/糊。

修法：`_qwen_vl_conditioning` 在 `keep_vision is None` 时直接用 tokenizer 的原生默认 **False**，
不再自己推断；原生 2.1 节点显式传的值照旧原样透传。

### 3. 这次不用换节点

上一版建议你改用 `TextEncodeQwenImage21`，但你在本机用 **2.0 的「编辑 Plus」+ SD3 空 latent**
跑出来的图是正常的 —— 说明你这套 `qwen_image_2.1` UNet 就是吃 16 通道 latent 的，
那就别再动拓扑了，**只需要把服务端 exe 和插件换成这一版**。

### 4. 实测（本机）

- `nodes.py` / `backend.py` `py_compile` 通过
- 单测：`tokenize` 后 `_text_of` 返回完整原文（66 字符），发给服务端的 payload：
  `text` 与原文完全一致、`images` 2 张、`no_resize=True`、`llama_template` 透传、
  `keep_vision` 不再被强塞（交给服务端按默认 False 处理）
- `RemoteCLIPVAE服务端.exe` 重新打包并 `--smoke` 通过

### 5. 换新后怎么确认生效

跑一次远程 CLIP 的工作流，服务端日志应出现：

```
[服务端] 图文混编：2 张图 / 2 个 vision token / keep_vision=False（客户端自带 reference_latents，服务端不再编）
```

只要 `keep_vision=False` 且提示词不是空的，出图就应该和本地 CLIP 一致。
如果还是糊，把这段日志和 `[服务端] 加载 CLIP ... (clip_type=...)` 那行发我。

---

## 上一版 v1.7.12 —— 「远程 CLIP 文本编码」退回纯文本版（服务端一律不动）

### 1. 节点回到 v1.7.6 的样子

`Remote CLIP 文本编码 (Remote CLIP Encode)` 上**不再有任何图片接口**：

- 去掉 `图片模式` 下拉
- 去掉 `图片1 … 图片32` 这一堆 IMAGE 输入
- 去掉 `vae` / `视觉模型` / `参考图像素` 三个可选输入

现在只剩：正向提示词、负向提示词、服务端显卡（只读）、服务端地址、模型、超时时间、
手动模型名、模型类型 + 可选 `clip` / `token` / `device`，输出照旧「正向条件 / 负向条件」两路。

### 2. 前端脚本同步收口

`web/refresh.js` 里那套「接一张图就自动多出一个空槽」（`syncImgSlots` / `onConnectionsChange` 增删槽）
一并删掉，避免对着已经没有 `图片N` 输入的节点做无意义增删；刷新模型列表 / 显卡只读 / 地址改后自动重探全部保留。

### 3. 服务端 / 其他节点：一行代码都没改

`model_server`（`app.py backend.py codec.py config.py panel.py gui.py`）与
`RemoteCLIPLoader` / `RemoteVAELoader` / `RemoteVAEDecode` / `RemoteVAEEncode` 全部保持 v1.7.11 原样，
所以包里的 `RemoteCLIPVAE服务端.exe` 仍是上版那个，**无需重新打**（v1.7.11 的 clip_type 自动识别照常生效）。
服务端的 `clip_encode` 依然保留 images / keep_vision / 图文混编能力，只是客户端节点不再发图。
之前用旧工作流存过「文本编码节点接了图」的，打开后那几个图片连线会被丢掉，需要重连到原生编码节点。

### 4. 实测（本机）

- `nodes.py` `py_compile` 通过；`refresh.js` `node --check` 通过
- 临时 ComfyUI(8196) + mock 服务端(8197)：`/object_info/RemoteCLIPTextEncode`
  → required = 正向提示词 / 负向提示词 / 服务端显卡 / 服务端地址 / 模型 / 超时时间 / 手动模型名 / 模型类型；
  optional = `clip` / `token` / `device`（**无任何 IMAGE 输入**）
- 提交 `RemoteCLIPLoader → RemoteCLIPTextEncode → PreviewAny` 图：`node_errors: {}`、`status: success`

---

## 上一版 v1.7.11 —— 根因抓到了：`clip_type=STABLE_DIFFUSION`（Qwen-VL 被当成 SD1.5 加载）

### 1. 你日志里那一行是关键

```
[服务端] 加载 CLIP qwen3vl_8b_bf16: [...qwen3vl_8b_bf16.safetensors] (clip_type=CLIPType.STABLE_DIFFUSION)
[服务端] 警告：只命中 0/2 个 vision token
```

`clip_type` 是 `stable_diffusion` → ComfyUI 用 **SD1.5 的 CLIP/tokenizer** 去加载这个 Qwen3-VL 8B 文件，
tokenizer 根本不认 `<|image_pad|>`，图当然进不去，条件里 0 个 vision token。
之前面板「扫描」会把新加的 CLIP 条目统一写成 `"clip_type": "stable_diffusion"`，就是这个来源。

### 2. 修复：clip_type 自动识别（不再写死 stable_diffusion）

- 服务端加载 CLIP 时，如果条目没写 / 写的是占位默认 `stable_diffusion`，会**按权重内容 + 文件名**判断：
  - safetensors 参数名里有 `model.visual.*` / `visual.*`（Qwen-VL 系带视觉塔）→ `qwen_image`
  - 有 `conditioner.*` → `flux`
  - 文件名/条目名含 `qwen` 且含 `vl` / `qwen_image` → `qwen_image`
  - 都不匹配 → 保持原值（真 SD1.5 不受影响）
- 日志会明确告诉你：`[服务端] 自动识别 clip_type=qwen_image（qwen3vl_8b_bf16.safetensors 看起来是 qwen_image 文本编码器）`
- 0 个 vision token 时的警告也改成了直接指路（提示去核对 clip_type）
- 面板扫描 / config 自动生成**不再写死** `clip_type`，交给加载时判断

### 3. 你这次要做的

1. 服务端换新 exe（`RemoteCLIPVAE服务端.exe`），重新启动；
2. 再跑一次，日志应出现 `自动识别 clip_type=qwen_image` 和
   `图文混编：2 张图 / 2 个 vision token / keep_vision=False，挂 2 份 reference_latents`；
3. 如果还想手动钉死，也可以在 `server/config.json` 里给该条目加 `"clip_type": "qwen_image"`。

⚠️ 另外两条仍然成立（上一版说过）：
- 你的节点是 **Qwen-Image 2.0** 的 `TextEncodeQwenImageEditPlus`（vision 缩 384²、latent 缩 1024²），
  配 `qwen3vl_8b_bf16`（2.1）时槽位和 latent 对不齐，即使图进去了也会有糊感 —— 请改用
  原生 **`TextEncodeQwenImage21`**，或用我们的「远程 CLIP 文本编码」+ Qwen-VL 图文混编模式；
- **别用 SD3 的空 Latent** 给 Qwen-Image（通道数/1:16 缩放都不对），用编辑节点自带的 `latent` 输出。

### 4. 实测（本机）

- 识别逻辑单测：`qwen3vl_8b_bf16.safetensors` → `qwen_image`；`clip_l.safetensors` → 空（不动）；
  伪造 `model.visual.*` 头的 safetensors → `qwen_image`；伪造 SD1.5 头 → 空
- `backend.py / config.py / panel.py / gui.py` 全部 `py_compile` 通过

---
## 本次更新 v1.7.10 —— 修「用原生文本编码节点 → 出图糊」：images / keep_vision 被丢掉了

### 1. 根因

你在工作流里用的是 ComfyUI **原生**节点「文本编码 Qwen Image 编辑 Plus」
（`TextEncodeQwenImageEditPlus`），它内部是这样调我们给的 RemoteCLIP 代理的：

```python
tokens = clip.tokenize(image_prompt + prompt, images=images_vl, llama_template=llama_template)
conditioning = clip.encode_from_tokens_scheduled(tokens)
```

而 v1.7.9 的 `tokenize()` 只留了文本，`images=` / `llama_template=` / `keep_vision=` **全被丢掉**，
于是服务端拿到的是「纯文字、没有图」的请求：

- vision 槽全是空的 → tokenizer 里没有 image token、`image_slots` 也没有；
- 但 VAE 那一路是正常的（`vae.encode()` 确实把参考 latent 编出来了）；
- 结果：DiT 收到一堆参考 latent，却不知道它们该插在序列的哪里 → 只能硬塞 → 出图糊/重影。

### 2. 修复

- `RemoteCLIP.tokenize(text, images=…, keep_vision=…, prevent_empty_text=…, llama_template=…)`
  现在把 **图片（PNG 编码，带实例内缓存，同一批图只编一次）**、`keep_vision`、`llama_template` 一起发给服务端；
- `encode_from_tokens_scheduled()` 发现是带图的 token 对象时，原样转发，并带 `no_resize=True`
  （原生节点已经把图缩好了，服务端不再二次缩放，避免尺寸被改）；
- 服务端 `clip_encode` 新增 `no_resize` / `keep_vision` / `prevent_empty_text` / `llama_template`：
  走的是 `clip.tokenize(text, images=…, keep_vision=…, llama_template=…)`，和原生节点完全一致。

服务端日志会打：`图文混编：N 张图 / N 个 vision token / keep_vision=False，挂 N 份 reference_latents`。

### 3. 还有一半原因：这个节点是 Qwen-Image **2.0** 的

`TextEncodeQwenImageEditPlus` 的缩放口径是 **vision 图缩到 384²，参考 latent 缩到 1024²**（2.0 的写法）。
你的 `qwen3vl_8b_bf16` 在 ComfyUI 里走的是 **Qwen-Image 2.1** 的 tokenizer
（`QWEN3VL_8B` → `qwen_image21.QwenImage21Tokenizer`），2.1 的要求是
**vision 和 latent 用同一套缩放，且都是 32 的倍数**，这样每个 vision slot 正好覆盖 2×2 个 latent。
384² 的 vision 配 1024² 的 latent，槽位和 latent 对不齐，画面就会糊、重影。

所以配 2.1 的模型，请用下面任一种：

| 用法 | 说明 |
|---|---|
| 原生 `TextEncodeQwenImage21`（节点名「文本编码 Qwen Image 21」） | 它是按 2.1 写的：vision/latent 统一缩放，还输出 positive/negative/latent 三路 |
| 我们的「远程 CLIP 文本编码」+ `图片模式 = 图片 → 服务端文本编码器（Qwen-VL 图文混编）` | 服务端内部已经按 2.1 官方口径缩放，接上「远程 VAE 加载器」就自动挂 reference_latents |

另外：**别用 SD3 的「空 Latent 图像」给 Qwen-Image**（通道数和 1/16 的缩放都对不上），
用上面这些节点自带的 `latent` 输出，尺寸按参考图来。

### 4. 实测（本机）

- `py_compile` 通过，已同步到本机 custom_nodes
- 直接调代理（模拟原生节点写法）：`clip.tokenize(带 "Picture 1: <|vision_start|>…" 的文本, images=[图1, 图2], llama_template=…)`
  → `tokenize` 载荷含 `images/keep_vision/llama_template/prevent_empty_text`，
  `encode_from_tokens_scheduled` 返回 `(1, 205, 768)`（77 + 64×2，**说明两张图真的到了服务端**）
- mock 服务端 + 临时 ComfyUI（8196）跑**原生节点** `TextEncodeQwenImageEditPlus`
  （clip=RemoteCLIPLoader、vae=RemoteVAELoader、image1/image2=EmptyImage）：
  `node_errors: {}`、`status: success`
- 真 Qwen3-VL 权重本机没有，最终清晰度要你在 10.10.10.2 上确认

---
## 本次更新 v1.7.9 —— 修正：Qwen-Image 编辑的图片走「文本编码器」，按官方 QwenImage21 节点实现

### 1. 上一版为什么会警告

v1.7.8 里我自己拼了 `<|im_start|>` 对话模板，还自己判断「图有没有变成 vision token」。
你那个 `qwen3vl_8b_bf16` 在 ComfyUI 里走的其实是 **Qwen-Image 2.1 的 tokenizer**
（`comfy/sd.py`：`clip_type=qwen_image` 且 `te_model=QWEN3VL_8B` → `qwen_image21.QwenImage21Tokenizer`），
它的机制和 2.0 不一样：

- 模板由 tokenizer 自己生成，多图时它会把 `<|vision_start|><|image_pad|><|vision_end|>` 按图数重复；
- 编码后如果**没有** VAE latent，需要 `keep_vision=True` 把 vision token 留在条件里；
- 如果**有** VAE latent，需要 `keep_vision=False` —— vision token 被摘掉，换成 `image_slots`（告诉 DiT 第几个位置插参考 latent）。

上一版既手拼模板又没传 `keep_vision`，还用了错误的 token 结构去判断，所以打了一条误报警告。

### 2. 现在的做法：完全照抄官方节点

`comfy_extras/nodes_qwen.py`（Qwen-Image 2.1 的参考图文本编码节点）就是这么干的，服务端现在一模一样：

1. 每张参考图按 `resolution²` 等比缩放到 **32 的倍数**（`resolution = √参考图像素`，默认 1024²）；
2. 同一张图既给文本编码器（vision 槽），又在服务端用 VAE 编一份 latent；
3. `clip.tokenize(text, images=…, keep_vision=len(ref_latents)==0, prevent_empty_text=True)`；
4. 有 latent 就 `reference_latents` 追加进条件（和官方 `conditioning_set_values(..., append=True)` 等价）。

所以现在：

- **接了「远程 VAE 加载器」** → 条件里带 `image_slots` + `reference_latents`，DiT 把参考 latent 拼进序列（**官方编辑用法**）
- **没接 VAE** → `keep_vision=True`，图像特征直接留在文本条件里（轻量用法）

服务端日志会直接告诉你走了哪条、以及建议的采样尺寸：

```
[服务端] 图文混编：2 张图 / 2 个 vision token，挂 2 份 reference_latents（采样尺寸建议 1184x896）
[服务端] 图文混编：2 张图 / 2 个 vision token，未接 VAE → keep_vision=True（图像特征直接进条件）
```

采样尺寸请按这条日志的建议来（官方节点也强调：采样尺寸偏离参考图尺寸，编辑会跑偏）。

### 3. 参数变化（节点上不用动）

- 「参考图最大像素」(MP) 含义不变（默认 1.00 = 1000²≈1MP），现在它就是官方的 `resolution`，同时决定文本编码器和 VAE 的缩放，保证每个 vision slot 正好覆盖 2×2 个 latent。
- 图文混编不再需要你在节点里手动挂 VAE 输出——「远程 VAE 加载器」接上就自动带上（服务端会自己加载那个 VAE）。

### 4. 实测（本机）

- `py_compile` 通过；`nodes.py` / `codec.py` 已同步到本机 custom_nodes
- 逻辑单测（伪 clip/vae 替身）：两图 + VAE → tokenize 收到 `keep_vision=False`、条件含 `image_slots` + 2 份 `reference_latents`；不接 VAE → `keep_vision=True`
- mock 服务端 + 临时 ComfyUI（8196）：`图片模式=Qwen-VL 图文混编` + VAE 接上 → `node_errors: {}`、`status: success`，客户端日志 `附带 ['reference_latents']`
- 真 Qwen3-VL 权重本机没有，tokenizer 的真实路径需要你在 10.10.10.2 上再跑一次

### 5. 顺带

- 服务端控制台/网页面板里的「A 端 / B 端」字样统一改成「客户端 / 服务端」。

---
## 本次更新 v1.7.7 —— 文本编码支持输入图片（自动增槽）+ 服务端图片解码提速

### 1. 「远程 CLIP 文本编码」多了图片接口，接一张自动长一个

- 节点新增 **`图片模式`** 下拉（默认「关闭（只编码文字）」，不接图时完全和以前一样）：

| 图片模式 | 服务端干什么 | 适合的模型 |
|---|---|---|
| 关闭（只编码文字） | 图片被忽略，只编码文字 | 所有普通文生图模型 |
| 参考图 → 服务端 VAE 编码（Qwen/Flux Edit 式） | 每张图在服务端用 VAE 编码成 latent，挂到条件的 `reference_latents` 上 | Qwen-Image-Edit、Flux Kontext 这类编辑模型 |
| 图片 → 服务端 CLIP 视觉（Redux 式） | 每张图在服务端用 clip_vision 编码，挂到条件的 `clip_vision_output` 上 | Flux Redux、IPAdapter 这类参考图模型 |

- 图片接口是**动态增槽**：节点上只显示 `图片1` 一个空接口，**每接上一张图，后面立刻自动多出一个空接口**
  （最多 32 个，拔掉图后多余的空槽会自动收回去）。新建节点/载入工作流都会自动整理成「接了几张 + 1 个空槽」。
- 配合两个新字段：
  - `vae`（可选输入）：图片模式 = VAE 编码时接「远程 VAE 加载器」的 vae——编码在服务端做，本机不加载 VAE。
  - `视觉模型`：图片模式 = CLIP 视觉时用，填服务端 clip_vision 目录里的文件名；**留空 = 自动用目录里的第一个**。
  - `参考图最大像素`：百万像素(MP) 上限（默认 1.00 ≈ 1MP，服务端按它等比缩放；0 = 原尺寸）。
- 服务端新增第三类模型目录 **clip_vision**：默认扫 `<comfyui_dir>/models/clip_vision`，
  放在别处就在 `config.json` 写 `"clip_vision_model_dir": "D:/models/clip_vision"`。
  `1_检查环境并生成配置.bat` 现在也会扫这个目录（`models/clip/` 里混着的 `EVA02_*` 这类视觉编码器
  会被自动归到 clip_vision，不再被当成文本编码器）。

实测（临时 ComfyUI 8196 + 服务端 mock 8197，`EmptyImage → RemoteCLIPTextEncode(图片1/图片2) → PreviewAny`）：

| 模式 | 结果 |
|---|---|
| 参考图 → VAE 编码 | `node_errors: {}`、`status_str: success`，日志 `参考图1 → 服务端 latent (1,4,64,64)`，条件额外键 `reference_latents`（2 条 latent） |
| 图片 → CLIP 视觉 | 同上，日志 `图片1 → 服务端视觉特征 (1,81,64)`，条件额外键 `clip_vision_output`（image_embeds 2×81×64 + penultimate 2×729×64） |
| 关闭 | 图片被忽略，条件里只有 `pooled_output` |

### 2. 服务端图片解码/编码重写（快 + 稳）

旧实现是逐帧串行 `base64.b64decode → Image.open → np.stack → 整体 astype(float32)`，
帧一多就明显卡在 CPU 上。新版 `codec.py`：

- **线程池并行**多帧解码/编码（`max(2, min(8, CPU 核数))` 个线程），两端 `codec.py` 仍逐字节一致；
- `np.asarray` 零拷贝 + 先按 uint8 堆叠，再一次性 `from_numpy().float().div_(255)`，少一次大张量拷贝；
- 支持灰度 / LA / P / PA / CMYK / YCbCr / 16bit（`I;16` → 8bit）/ float 图，
  多帧通道数或尺寸不一致时自动用最近邻统一（不再因为一张图格式不同就整条失败）；
- `Image.open` 一律 `with` 显式关闭，Encoding 用 `compress_level=3`（比默认 6 快、体积几乎一样）。

实测（4 帧 1024×1024）：**编码 0.131s / 解码 0.038s，往返最大误差 0.00196（< 1/255）**，
单帧 selftest（灰度、RGBA）全过。

---

## 本次更新 v1.7.4 / v1.7.5 / v1.7.6 —— 节点按截图改版 + 修校验 + 直连自带节点

- 节点标题：`远程 CLIP 加载器 (Remote CLIP Loader)` / `远程 VAE 加载器 (Remote VAE)` /
  `远程 CLIP 文本编码 (Remote CLIP Encode)`；分类统一为 **`comfyui_remote_clip_vae`**，
  节点脚标也是它。**全文不再出现「A 端 / B 端」字样**，服务端日志里的 `[B]` 也改成了 `[服务端]`。
- 字段顺序按截图：服务端显卡 → 服务端地址 → 模型 → 超时时间 → 手动模型名（→ 模型类型），
  最后一行整宽按钮 **「🔄 刷新模型列表」**。
- 「服务端显卡」改为**只读灰字**：点刷新时自动填成 `显卡型号 显存 · 计算方式`（如 `RTX 4070 12.0GB · CUDA`），
  连不上显示「（连不上服务端）」，手改会被还原。
- 文本编码节点一个节点出**正向条件 / 负向条件两路**，直接接 KSampler（省掉原来的两个编码节点）。
- 「模型类型」现在真的生效：选 `flux` / `sd3` / `qwen_image` 时会把 clip_type 传给服务端，
  服务端发现类型变了会自动卸载旧的再按新类型重装。
- 附带保留 `远程 VAE 解码` / `远程 VAE 编码`（同风格命名，去掉了 vae 句柄没有消费者的死路）。

### v1.7.5 修「无效输入：模型」这个报错

现象：下拉里明明识别到了模型，点运行却红条报 **无效输入 · 远程 VAE 加载器 - 模型 / 远程 CLIP 文本编码 - 模型**。

原因：ComfyUI 在**启动那一刻**就固化了每个下拉框的合法值列表（`INPUT_TYPES` 的返回值），
而「模型」是点「🔄 刷新模型列表」时从服务端实时拉的——新名字不在启动时那份快照里，
后端队列校验就判 `value_not_in_list`，跟模型本身能不能用无关。

修法（两条）：
1. 三个节点都加了 `VALIDATE_INPUTS(模型=..., 模型类型=...)`，**跳过这份白名单校验**，
   只拦明显是占位符的值（`（连接失败，检查服务端）` 之类），剩下的交给执行阶段报清楚的中文错误；
   顺手兼容了旧版标签写法（`flux（Flux 的 t5xxl / clip_l）` → `flux`）。
2. 下拉清单加了本地缓存 `model_cache.json`：只要成功拉过一次，ComfyUI 重启后即使服务端还没开，
   下拉里也是真名字，不会再退回占位符。

实测：起一个临时 ComfyUI（服务端故意不开，启动时下拉只有占位符），把 `模型` 填成启动列表里
根本没有的 `my_flux_vae.safetensors` 直接 `/prompt` 提交——返回 `node_errors: {}` 正常排队，
之后的失败是执行阶段「连不上服务端」的中文报错，不再是「无效输入」。

### v1.7.6 加载器输出改成真 `CLIP` / `VAE` 类型，可以直连自带节点

之前「远程 CLIP 加载器」输出的是自定义类型 `REMOTE_CLIP`，只能接本套件自己的节点，
接不了 ComfyUI 自带的 `CLIPTextEncode`。现在改成输出**鸭子类型的代理对象**，
`RETURN_TYPES` 就是 `CLIP`（VAE 加载器同理，就是 `VAE`）：

| 加载器 | 输出类型 | 现在能直接接 |
|---|---|---|
| 远程 CLIP 加载器 | `CLIP` | 自带 `CLIPTextEncode`、`CLIPSetLastLayer`、`CLIPTextEncodeSDXL`… |
| 远程 VAE 加载器 | `VAE` | 自带 `VAEDecode`、`VAEEncode`、`VAEDecodeTiled`、`VAEEncodeTiled`、`VAEEncodeForInpaint`… |

代理对象实现的是原生节点真正会调的那些方法（`tokenize` / `encode_from_tokens_scheduled` /
`encode_from_tokens` / `clip_layer` / `clone`，`decode` / `encode` / `decode_tiled` / `encode_tiled` /
`spacial_compression_decode` / `throw_exception_if_invalid` …），内部全部转成 RPC 发给服务端，
本机不加载任何模型。所以两种用法随便挑：

```
用法一（和原生一模一样）：
  远程 CLIP 加载器 ── clip ─→ CLIPTextEncode(正向) ─→ KSampler positive
                          └→ CLIPTextEncode(负向) ─→ KSampler negative
  远程 VAE 加载器  ── vae  ─→ VAEDecode ─→ PreviewImage / SaveImage

用法二（少连几根线）：
  远程 CLIP 文本编码（正向/负向一个节点出两路）→ KSampler
  远程 VAE 解码 / 编码（等价于自带 VAEDecode / VAEEncode）
```

实测（真 ComfyUI，服务端 mock）：`远程 VAE 加载器 → 原生 VAEDecode → SaveImage` 真的写出了
13.7KB 的 PNG；`远程 CLIP 加载器 → 原生 CLIPSetLastLayer → 原生 CLIPTextEncode` 提交后
`node_errors: {}`（类型/连线校验通过），编码结果 1×77×768 + pooled 1×1280。
本套件自己的编码/解码节点在「接 / 不接 clip 句柄」两种情况下也都跑通。


---

## 一、目录结构

```
RemoteCLIPVAE/
  RemoteCLIPVAE服务端.exe       ★★ 双击就开（Windows 桌面程序，不需要装 python）
  0_启动图形界面.bat            ★ 图形控制面板：左边配置/状态，右边黑底实时日志
  1_检查环境并生成配置.bat     探测 ComfyUI 目录/Python，扫描模型，生成 config.json
  2_启动服务端.bat             用上一步记录的 Python 启动（可加 --mock 联调）
  3_安装插件到ComfyUI.bat      把 comfyui_remote_clip_vae 拷进 custom_nodes，并写入服务器地址
  4_用ComfyUI的Python启动服务端.bat  指定 ComfyUI 目录启动（run_headless.py 方式，日志留在控制台）
  5_自测.bat                   不启动 ComfyUI，直接跑一遍 CLIP→VAE 全链路

  comfyui_remote_clip_vae/     ← 装到 出图电脑 custom_nodes 的插件
    __init__.py                节点注册 + WEB_DIRECTORY
    nodes.py                   5 个节点
    api.py                     本机代理路由 /remote_clipvae/probe（刷新按钮用）
    web/refresh.js             「🔄 刷新模型列表」按钮、显卡栏只读
    codec.py                   张量/图像序列化协议（与 model_server/codec.py 完全一致）
    remote_api.py              HTTP 客户端（只用标准库）
    settings.py                默认服务器地址读取
    default_server.json        服务器地址（3_ 脚本会帮你写）
    model_cache.json           ← 自动生成：上次成功拉到的模型清单（重启后下拉不丢）

  model_server/                ← 在 服务端电脑运行
    app.py                     HTTP 服务（/health /models /rpc）
    backend.py                 CLIP/VAE 加载、编解码、LRU、空闲释放
    config.py                  配置读写 + 模型自动扫描
    codec.py                   同上（两端必须逐字节一致）
    config.json                唯一需要改的配置
    config.example.json        Flux/SDXL/SD1.5/SD3/Qwen 的写法参考
    gui.py                     ★ 桌面版控制面板（tkinter，只用标准库）
    panel.py                   ★ 网页面板（没有 tkinter 时的自动替换形态）
    _python.txt                ← 面板/1_ 生成：记住 ComfyUI 的 python（2_/4_/5_ 直接复用）
    _comfy.txt                 ← 面板/1_ 生成：记住 ComfyUI 目录（4_ 直接复用）
    run_headless.py            --backend comfy --comfy-path ... 兼容入口
    selftest.py                5_ 脚本调用的自测
```

---

## 二、图形界面（推荐先跑这个）

**最省事的用法：直接双击 `RemoteCLIPVAE服务端.exe`**（Windows 桌面窗口，自带 tkinter 运行时，
机器上有没有 python、装没装 ComfyUI 都无所谓，能打开）。打开后在界面里填四样东西：

| 设置项 | 填什么 |
|---|---|
| ComfyUI 路径 | 服务端电脑上 ComfyUI 的源码根目录（能看到 `comfy` 文件夹和 `main.py` 的那层），点「浏览...」选 |
| 本机 ComfyUI 的 Python | **选完上一行就自动填好**：按这个 ComfyUI 目录找 `python_embeded\python.exe` / `.venv\Scripts\python.exe`（含上一层目录的 `.venv`），并优先挑里面带 torch 的那个；找不到再手动「浏览...」 |
| CLIP 模型目录 | 文本编码器所在文件夹，例如 `<ComfyUI>\models\text_encoders`（也可以选 `models\clip`，或 ComfyUI 外面的任何目录） |
| VAE 模型目录 | VAE 所在文件夹，例如 `<ComfyUI>\models\vae` |
| 视觉编码器目录（`clip_vision`） | 只有用「图片 → CLIP 视觉」模式才需要：默认 `<ComfyUI>\models\clip_vision`；放在别处就在 `config.json` 写 `"clip_vision_model_dir": "D:/models/clip_vision"`，界面里不需要选 |

> **CLIP / VAE 两栏只认你填的目录**：改「ComfyUI 路径」不会去动这两栏，扫描也只扫这两个目录里的模型文件
> （不会再从 `<ComfyUI>\models` 下面另外猜一遍）。
> **不用手点扫描**：改 CLIP / VAE 目录（含「浏览...」选）后立刻扫描，列表马上出现条目
> （`clip/` 里的 `EVA02_CLIP_*` 这类视觉编码器会自动跳过，避免加载时报错）。
> 服务已经开着时再扫描/加目录，界面会自动让服务端重读 `config.json`（走 `/reload`），**不用重启服务**。

**exe 是单文件、自带服务端**：`app.py` / `backend.py` / `codec.py` / `config.py` 都已经打进 exe 里，
运行时解到临时目录再交给 ComfyUI 的 python 跑。所以：

- 把 `RemoteCLIPVAE服务端.exe` **单独拖到任何文件夹（甚至桌面）都能直接双击用**，不需要 `model_server` 目录在旁边
- 配置写在 **exe 同一目录**的 `config.json`（外加 `gui_state.json`，记录上次填的四个路径）；
  万一 exe 放在不可写的位置（如 `C:\Program Files`），会自动改用 `%APPDATA%\RemoteCLIPVAE\config.json`
- 想确认自带的服务端是不是完整：`RemoteCLIPVAE服务端.exe --smoke`，退出码 0 且同目录生成 `_smoke.txt`，
  里面会写 `app.py/codec/backend : OK` 和 `srv_dir` 的实际位置

（包里的 `model_server` 目录保留给 `2_` / `4_` / `5_` 这些 bat 和网页面板那条路线用，**exe 用不到它**。）

填完点「启动服务」即可。全部设置存在 `model_server\config.json`，包里**没有任何写死的路径**，换机器就是重新填这四个框。

也可以双击 `0_启动图形界面.bat`（有 exe 时它直接帮你打开 exe），出来的就是同一个控制面板：左边「服务配置 / 运行状态 / 服务端 CLIP 模型 / 服务端 VAE 模型」，
右边「本机日志 / 服务端日志（实时）」两个页签 + 黑底实时控制台 + 清屏。

| 区域 | 内容 |
|---|---|
| 服务配置 | 监听地址、端口（**计算设备不在界面里选**：一律自动，NVIDIA / AMD / Intel 都交给 ComfyUI+torch 判断，状态栏会显示实际用的是哪个）、CLIP 模型目录、VAE 模型目录、ComfyUI 路径、本机 ComfyUI 的 Python、勾选「只读这两个目录」；按钮 `启动服务` `停止服务` `清空显存(卸载模型)` `刷新模型列表` `打开目录` |
| 运行状态 | 计算方式、加载后端、设备（显卡名）、显存 已用/总量、GPU 利用率、CPU、torch 已用、内存；显存与 GPU 利用率两条进度条实时刷新 |
| 服务端 CLIP / VAE 模型 | 两个列表（扫描出来的条目，可 Ctrl 多选），`预加载选中的` 直接把模型装进 服务端显存，底部显示当前已加载 |
| 日志 | `本机日志`（面板自己记的启动/失败原因）+ `服务端日志（实时）`（服务端进程 stdout/stderr 一行行进来，带颜色） |

两种形态，自动挑：

- 本机 python **有可用的 tkinter** → 桌面窗口（`model_server/gui.py`；有 `pythonw.exe` 就用它启动，不带黑框）
- **没有 tkinter**（整合包 `python_embeded` 常被精简掉）→ 本地网页面板（`model_server/panel.py`），
  用 Chrome / Edge 的 `--app=` 打开成独立窗口；面板监听 `127.0.0.1:8190`（被占用自动 +1，最多试 20 次），
  关掉它留下的黑框窗口 = 服务端一起停

> 界面用什么 python 跑无所谓：**服务端永远是用 ComfyUI 的 python 起子进程**（对应面板里「本机 ComfyUI 的 Python」和「优先用 ComfyUI 自带的 Python」两项）。

---

## 三、5 步跑起来

### 第 1 步：服务端电脑上 `1_检查环境并生成配置.bat`

- 把 **ComfyUI 源码根目录**拖进窗口（那个里面能看到 `comfy/` 文件夹和 `main.py` 的目录，
  整合包路径通常是 `...\ComfyUI_Mie_xxx\ComfyUI_Mie_xxx\ComfyUI`）
- 脚本会自动找到 ComfyUI 自带的 `python_embeded\python.exe`
- 然后扫描 `models\` 并写入 `config.json`，同时打印扫描到的 VAE / 文本编码器
- 顺带生成两个小文件：`model_server\_python.txt`（记住 python 路径）、`model_server\_comfy.txt`
  （记住 ComfyUI 目录）。后面 2_/4_/5_ 会自动读它们，不用再拖一次路径

> 本包**故意不建 venv**：服务端必须用 ComfyUI 自己的 Python，因为要 `import comfy.sd`。
> 用独立 venv 会报 `No module named 'comfy_aimdo'` 或者能跑但数值对不上。

### 第 2 步：`2_启动服务端.bat`

```
==============================================================
 服务端模型服务已启动
 监听：http://0.0.0.0:8199
 本机节点里填：http://192.168.3.2:8199
==============================================================
```

> v1.6.2 起 `2_` 不再无脑抓 PATH 上的 `python`：它会先探测 `import numpy, torch`，
> 跑不通就打印 `[skip] ...` 跳过。PATH 上的 Microsoft Store 版 python 常装着残缺的 torch
> （报 `ModuleNotFoundError: No module named 'torchgen'`），那种 python 绝对不能跑服务端。
> 如果它一个可用的都没找到，会直接提示你三种做法（0_ 面板 / 1_ 生成配置 / 4_ 指定目录），
> 也可以当场把 ComfyUI 目录拖进去，它会自己挑 `python_embeded` / `python_embedded` / `python` / `.venv` / `venv`。
**先建议 `2_启动服务端.bat --mock`**：不加载真模型、返回随机张量，几十秒就能验证网络和节点接线。
启动时如果提示"以下模型文件的路径不存在"，照着改 `config.json` 即可。

### 第 3 步：出图电脑上 `3_安装插件到ComfyUI.bat`

- 把 出图电脑的 `custom_nodes` 目录拖进窗口
- 脚本会问服务器地址（服务端窗口顶部那行 `http://192.168.x.x:8199`），直接写进 `default_server.json`
- 重启 ComfyUI，节点菜单里出现分类 **`comfyui_remote_clip_vae`**

### 第 4 步（可选）：`4_用ComfyUI的Python启动服务端.bat`

和第 2 步是同一个服务端，只是改成"指定 ComfyUI 目录 + `run_headless.py --backend comfy --comfy-path`"的写法，
适合把服务端和 ComfyUI 装在不同盘、第 1 步没记录到 Python 的情况。

用法三种都行：

```
4_用ComfyUI的Python启动服务端.bat                       ← 直接回车，用第 1 步记下的目录
4_用ComfyUI的Python启动服务端.bat "F:\xxx\ComfyUI"      ← 当场指定目录
4_用ComfyUI的Python启动服务端.bat --mock                ← 只带参数，目录照样从 _comfy.txt 读
```

### 第 5 步：`5_自测.bat`

不启动 ComfyUI，直接按节点背后的逻辑跑：`/health` → CLIP 编码 → VAE 解码 → VAE 编码。
看到 `[OK] 全链路通过` 就说明两台机器、协议、模型全部就绪。

---

## 四、工作流怎么接

```
原生：  CheckpointLoaderSimple ─┬─ CLIP ─→ CLIPTextEncode ─────────┐
                               ├─ VAE  ─→ VAEDecode ─→ SaveImage  │
                               └─ MODEL ──────────────→ KSampler ─┘

改造：  Load Diffusion Model（只加载 UNet）────────────────→ KSampler
        远程 CLIP 加载器 → 远程 CLIP 文本编码 ───────────────→ KSampler(positive / negative 两路)
        远程 VAE 加载器 → 远程 VAE 解码 ────────────────────→ PreviewImage / SaveImage
```

⚠️ **最容易踩的坑**：本机如果继续用 `CheckpointLoaderSimple` / `Load Checkpoint`，
它会把 checkpoint 里自带的 CLIP 和 VAE **一起加载进 本机的显存** —— 那就没省到。
真正省显存的用法是 本机用 `Load Diffusion Model` 加载独立的 unet / diffusion_models 文件
（`flux1-dev.safetensors`、SDXL 的 unet 版等），CLIP 和 VAE 全部走 服务端。
如果手上只有整包 checkpoint，先用 ComfyUI 的"拆包/unet 提取"把 unet 单独存出来。

### 节点清单（分类：`comfyui_remote_clip_vae`）

| 节点 | 输入（从上到下） | 输出 | 说明 |
|---|---|---|---|
| **远程 CLIP 加载器** (Remote CLIP Loader) | 服务端显卡 / 服务端地址 / 模型 / 超时时间(5) / 手动模型名 / 模型类型 / 🔄 刷新模型列表 | `clip`（类型 **CLIP**） | 句柄式代理对象，本机不占显存；可直接接自带 CLIPTextEncode / CLIPSetLastLayer |
| **远程 VAE 加载器** (Remote VAE) | 服务端显卡 / 服务端地址 / 模型 / 超时时间(5) / 手动模型名 / 🔄 刷新模型列表 | `vae`（类型 **VAE**） | 同上（没有「模型类型」）；可直接接自带 VAEDecode / VAEEncode / VAEDecodeTiled |
| **远程 CLIP 文本编码** (Remote CLIP Encode) | 正向提示词 / 负向提示词（两个多行框）/ 服务端显卡 / 服务端地址 / 模型 / 超时时间(120) / 手动模型名 / 模型类型 / 🔄 刷新模型列表（可选输入 `clip`） | `正向条件` `负向条件` | 一个节点同时出正负两路 CONDITIONING，直接接 KSampler；不接 clip 也行，用它自己的字段 |
| **远程 CLIP 图文编码** (Remote CLIP Encode + Images) | 正向提示词 / 负向提示词 / 服务端显卡 / 服务端地址 / 模型 / 模型类型 / 参考图最大像素(MP)（可选 `clip` `vae` + `图片1..图片10`，接一张自动增槽） | `正向条件` `负向条件` | 图片既进 vision 槽又（接了 vae 时）编 `reference_latents`；`keep_vision` 自动按有无 vae 决定 |
| **提示词优化** (Prompt Optimizer, Remote) | 用户提示词 / 是否反推 / 使用远程模型 / 服务端地址 / API Key / 文生图模型 / 图生图模型 / 视觉模型 / 参考图最大像素 / 输出语言 / 透明背景 / 种子值 / 卸载模型（可选 `图片1..图片10`，接一张自动增槽） | `提示词` `负向提示词` `是否反推` | 不用本机加载 GGUF：开则调服务端 GGUF，关则用本机 GGUF；「是否反推」开关决定反推还是走系统优化提示词 |
| 远程 VAE 解码 (Remote VAE Decode) | vae（类型 VAE）/ latent / 超时时间(600) / 分块解码 / 分块尺寸 / 重叠 / 输出设备 | `图像` | 等价自带 VAEDecode，只是把解码放服务端 |
| 远程 VAE 编码 (Remote VAE Encode) | vae（类型 VAE）/ 图像 / 超时时间(600) / 分块编码 / 分块尺寸 / 重叠 | `latent` | 等价自带 VAEEncode，编码在服务端做 |

参数要点：

- **服务端显卡**：只读灰字，点「🔄 刷新模型列表」时自动填服务端型号 + 显存，连不上显示「（连不上服务端）」
- **模型**：服务端 config.json 里的条目名；连不上服务端时下拉是「（连接失败，检查服务端）」，不会让 ComfyUI 起不来
- **手动模型名**：填了优先于下拉（服务端条目名，不依赖网络也能跑老工作流）
- **模型类型**：`自动（按文件名判断）` = 用服务端 config.json 里的 `clip_type`；Flux / SD3 / Qwen 对不上时可手动指定，服务端会按新类型重新装模型
- **超时时间**：加载器是连接检查超时（秒）；文本编码 / VAE 是单次调用超时，首次装大 CLIP 要留足（默认 120 / 600）
- **分块解码 / 编码**：服务端显存不够解大图时打开，走 `decode_tiled/encode_tiled`
- **输出设备**：VAE 解码默认 `cpu`（最省本机显存，SaveImage/PreviewImage 都支持）；文本编码 `auto` 与原生一致

---

## 五、config.json

| 字段 | 说明 |
|---|---|
| `server.host` / `server.port` | 监听地址与端口，默认 `0.0.0.0:8199` |
| `server.token` | 可选鉴权；填了之后 本机节点要填同一个 |
| `comfyui_dir` | **必改**，ComfyUI 源码根目录（含 `comfy/`） |
| `device` | `auto`（默认）/ `cuda`（强制显卡）/ `cpu`（强制纯 CPU）。**界面里已经没有这个选项了**，想强制就在 config.json 里手写；不写就是 auto，AMD / Intel / NVIDIA 显卡都交给 ComfyUI 自己判断 |
| `max_loaded.clip` / `.vae` | 同时驻留的模型数，超出按最久未用淘汰，默认各 1 |
| `idle_unload_seconds` | 空闲多少秒自动释放模型，0 = 不释放 |
| `models.clip` | 键名随便起（本机下拉框显示它）；`files` 可用多个文件（Flux 的 T5+CLIP-L）；`clip_type` 如 `flux`/`stable_diffusion`/`sd3`/`qwen_image` |
| `models.vae` | 同上，通常单个文件 |

`files` 写相对路径时按 `<comfyui_dir>/models/` 查找，也可以写绝对路径。
`config.example.json` 里有 Flux / SDXL / SD1.5 / SD3 / Qwen-Image 的现成写法。

---

## 六、接口（可直接 curl 调试）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 设备、显存、已加载模型、可用模型清单 |
| GET | `/models` | 模型清单 |
| GET | `/reload` | 重读 `config.json`（界面扫描/改模型目录后自动调，不用重启服务） |
| POST | `/rpc` | `{"op": "...", "params": {...}}` |

`op` 取值：`clip_encode` / `vae_decode` / `vae_encode` / `vae_info` / `load` / `unload` / `status` / `models`

```powershell
curl.exe -X POST http://127.0.0.1:8199/rpc -H "Content-Type: application/json" -d '{\"op\":\"unload\"}'
```

---

## 七、带宽与耗时（千兆局域网，实测）

| 操作 | 数据量 | 网络耗时 | 备注 |
|---|---|---|---|
| 文本编码 SD1.5（77×768） | ~0.2 MB | 5~10 ms | |
| 文本编码 Flux（512×4096+pooled） | ~8 MB | 60~90 ms | 显存吃紧可 `device=cpu` |
| VAE 解码 1024² | PNG 1.5~2 MB | 20~40 ms | 服务端显存紧就开 `use_tiled` |
| VAE 编码 1024² | PNG 1.5~2 MB | 20~40 ms | 同上 |

时间大头是 服务端**首次装模型**（SDXL CLIP 约 15~60 s，Flux 的 T5-XXL 更久），之后走缓存。

---

## 八、已验证环境与实测数据

本包在下面这套环境里跑过（真模型、非模拟）：

- 服务端：Windows + ComfyUI Mie 整合包（Python 3.13、torch 2.10.0+cu130、RTX 5070）
- 实测：`context = 1×77×768`、`pooled_output = 1×768`（从 checkpoint 抽出的 CLIP）
- `flux2_vae`（4 维，128 通道，16× 下采样）：解码 → `IMAGE 1024×1024×3`；编码 → `LATENT 128×64×64`
- `qwen_image_vae`（5 维视频 VAE，16 通道）：自动补 `T=1`，解码 → `512×512`，编码 → `16×64×64`
- 协议自测：张量往返完全一致，PNG 往返误差 ≤ 1/255
- `--mock` 全链路（health→编码→解码→编码）：0.4 s

---

## 九、排错对照表

| 现象 | 原因 / 解决 |
|---|---|
| 双击 `0_启动图形界面.bat` 弹出黑框又立刻消失 | 黑框就是网页面板进程，它会自己去开浏览器窗口；浏览器没起来就手动打开黑框里打印的 `http://127.0.0.1:819x/` |
| 面板能开，点「启动服务」后状态仍是「未运行」 | 看「本机日志」的红字：通常是记录的 python 不存在，点「自动找」重选 `python_embeded\python.exe` |
| 面板顶部给的地址不是你想要的网卡 | 多网卡（tailscale / 虚拟网卡）按 192.168 → 10 → 172 排序取第一个；要换就把「监听地址」改成具体 IP |
| 拖进「本机 ComfyUI 的 Python」后启动，报「还没指定 ComfyUI 的 python」或路径看着没错却不通 | Windows 拖拽会自动加双引号（`"D:\...\python.exe"`）。v1.6.1 起面板和 bat 都会自动去掉引号；旧包请手动删掉引号再点一次 |
| 用的是 ComfyUI Desktop（python 在 `ComfyUI\ComfyUI\.venv\Scripts\python.exe`） | 把「ComfyUI 路径」选到 `...\ComfyUI\ComfyUI`（里面有 comfy 和 main.py）即可，python 会自动识别（含 `.venv\Scripts`，也看上一层目录的 `.venv`），优先挑带 torch 的那个 |
| 选了模型目录，列表还是 0 条 | 先看「本机日志」里的扫描行：目录不存在会红字提示；目录里只有 `put_*.safetensors` 占位文件、或者都是 `clip_vision` / `EVA02` 视觉编码器时会被跳过（那些本来不能当文本编码器） |
| 「刷新模型列表」扫出 0 个条目 | Desktop 版 `models\vae`、`models\text_encoders` 常是空目录、模型放别处。把「CLIP 模型目录」「VAE 模型目录」填成真正的模型所在目录，再刷新 |
| 「运行状态」里设备显示 `—` | 服务还没起来；起来后会显示显卡名和显存实时占用 |
| 节点下拉框只有「（连接失败，检查服务端）」 | 看节点提示里的具体原因：服务端没启动 / IP 端口错 / 防火墙 / token 不一致 |
| 刷新失败：`Unexpected endpoint or method. (GET /health)` | **这个端口上不是本服务端**：要么「服务端地址」填到了 ComfyUI(8188) / LM Studio / Ollama 这类同机 HTTP 服务，要么端口号写错。用服务端面板标题上的 `http://<服务端IP>:8199`；面板「运行状态」必须是运行中；浏览器打开 `http://<IP>:8199/health` 应看到 `{"ok":true,...}` |
| 刷新失败：`无法连接服务端 ... 目标计算机积极拒绝` | 那个端口没人监听：exe 没点「启动服务」、IP/端口错、或防火墙没放行入站 TCP 8199 |
| `无法连接 服务端服务` | 两台机不在同一网段；`ping B的IP`，浏览器打开 `http://B的IP:8199/health` |
| 服务端启动就报 `No module named 'codec'` | 用了整合包 `python_embeded`（带 `._pth` 会丢脚本目录）—— 本包已在 `app.py` 里显式补 `sys.path`，别删那段 |
| `无法从 ... 导入 comfy 模块` | `comfyui_dir` 指错，要指到含 `comfy/` 和 `main.py` 的目录 |
| `CLIP 文件不存在` | `config.json` 里 `files` 路径错（相对路径是相对 `<comfyui_dir>/models/`） |
| `VAE is invalid: None` | 那个 checkpoint 里没有 VAE；换成独立 VAE 文件（`models/vae/*.safetensors`、`ae.safetensors`、`sdxl_vae.safetensors`） |
| `VAE 解码失败：latent 有 4 个通道，但该 VAE 需要 16 个` | 跨机模型不同源（SD1.5 的 latent 配了 Flux/Qwen 的 VAE） |
| KSampler 维度不匹配 / 出鬼图 | 同上，A 的 UNet 与 B 的 CLIP/VAE 必须同一代模型 |
| 服务端 VAE 解码 OOM | 勾 `use_tiled`，或调小 `tile_size` |
| 图像与本地解码略有差异 | PNG 8bit 量化，≤1/255，正常 |
| 第一次执行卡很久 | 服务端正在装模型，看 B 控制台；可把加载器 `preload` 打开提前装 |

---

## 十、安全

- 内网方案：`host` 用 `0.0.0.0` 时只在信任的局域网里用，**不要把 8199 映射到公网**
- 多人共用设 `server.token`
- 随时释放模型：`curl -X POST http://B的IP:8199/rpc -d "{\"op\":\"unload\"}"`，或等 `idle_unload_seconds`
