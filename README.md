# ComfyUI-RemoteCLIPVAE

双机 ComfyUI 分离套件的**整包版**：一个仓库、一次安装，同时得到两套节点。

- **remote_clip_vae/** —— 远程 CLIP / VAE / 文本编码 / 图文编码 / 提示词优化 / VAE 解码·编码
- **qwenvl_remote/** —— 远程 QwenVL 加载器 / QwenVL 图生文（含高级版）/ 提示词增强 / 文本净化 / 视频加载器
- **model_server/** —— RemoteCLIPVAE 服务端源码（另有 `RemoteCLIPVAE服务端.exe` 可直接双击跑）
- **web/** —— 两套节点的前端扩展（刷新按钮、动态图片口、视频列表刷新）

本机只跑 UNet/DiT 出图与前端；CLIP、VAE、QwenVL 全部推理放在另一台机器上的服务端，HTTP 传张量，不共享显存。

## 安装

```
ComfyUI/custom_nodes/ComfyUI-RemoteCLIPVAE/     ← 本目录（含 __init__.py）
```

或直接用 ComfyUI-Manager 搜 `RemoteCLIPVAE` 安装。装完重启 ComfyUI。

服务端（另一台机器）：跑 `RemoteCLIPVAE服务端.exe` 或 `2_启动服务端.bat`，默认端口 8199。

## 节点

远程 CLIP/VAE 侧（分类 `comfyui_remote_clip_vae`）：远程 CLIP 加载器、远程 VAE 加载器、远程 CLIP 文本编码、远程 CLIP 图文编码、提示词优化、远程 VAE 解码、远程 VAE 编码。

QwenVL 侧（分类 `🧪AILab/QwenVL 远程`）：QwenVL 远程加载器（🔄 刷新模型列表 / 🗑 卸载服务端模型）、QwenVL 视频加载器（🔄 刷新视频列表）、QwenVL 远程、QwenVL 远程·高级、QwenVL 提示词增强、QwenVL 文本净化。

两套节点的详细说明见 `remote_clip_vae/README_RCV.md` 与 `qwenvl_remote/README_QwenVL.md`；套件总说明见 `README_CLIPVAE套件.md`。

## 连接配置

两个插件目录里各有一份配置：`remote_clip_vae/default_server.json`、`qwenvl_remote/remote_server.json`，默认 `http://10.10.10.2:8199`，节点界面里也能直接改地址。

## 发布到 Comfy Registry

```powershell
# 1. 填发布者 ID（registry.comfy.org 个人页 @ 后面那一串，创建后不可改）
.\publish.ps1 -PublisherId 你的ID -Token "pat-xxxx"
# 只想校验元数据、不发布：
.\publish.ps1 -PublisherId 你的ID -SkipPublish
```

也可以走 GitHub Actions：把 PAT 存成仓库 Secrets `REGISTRY_ACCESS_TOKEN`，推 `pyproject.toml` 改动即自动发布（`.github/workflows/publish_action.yml`）。

发布前自检：`python selftest_local.py`（用 ComfyUI 的 python 跑，核对节点数/显示名/web 目录）。

## 许可

MIT，见 `LICENSE.txt`。
