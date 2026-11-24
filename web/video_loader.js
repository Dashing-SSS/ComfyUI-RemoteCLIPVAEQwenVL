// ===========================================================================
//  ComfyUI-QwenVL-Remote / web/video_loader.js
//
//  只服务 QwenVLVideoLoader（QwenVL 视频加载器）：
//    · 「🔄 刷新视频列表」按钮 —— 扫描「视频目录」里的视频文件，回填「视频文件」下拉
//    · 改「视频目录」后自动重扫（防抖 900ms）
//  扫描走本机路由 GET /qwenvl_remote/videos（见 api.py），不跨域、不联网。
// ===========================================================================

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE = "QwenVLVideoLoader";
const DIR_W = "视频目录";
const FILE_W = "视频文件";
const BTN = "🔄 刷新视频列表";
const EMPTY = "（目录里没有视频，填路径后点 🔄 刷新视频列表）";

function getWidget(node, name) {
  return (node.widgets || []).find((w) => w.name === name);
}

function grow(node) {
  try {
    const size = node.computeSize();
    node.setSize([Math.max(node.size[0], size[0]), Math.max(node.size[1], size[1])]);
    node.setDirtyCanvas(true, true);
  } catch (e) {}
}

function setCombo(widget, values, keep) {
  if (!widget) return;
  if (!widget.options) widget.options = {};
  widget.options.values = values;
  const cur = keep !== undefined ? keep : widget.value;
  widget.value = values.includes(cur) ? cur : values.length ? values[0] : "";
}

async function fetchVideos(dir, recursive) {
  const qs = `dir=${encodeURIComponent(dir || "")}${recursive ? "&recursive=1" : ""}`;
  try {
    const resp = await api.fetchApi(`/qwenvl_remote/videos?${qs}`, { cache: "no-store" });
    return await resp.json();
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e), videos: [] };
  }
}

async function scan(node, { silent = true } = {}) {
  const dirW = getWidget(node, DIR_W);
  const fileW = getWidget(node, FILE_W);
  const dir = ((dirW && dirW.value) || "").trim();
  const data = await fetchVideos(dir, false);
  const keep = fileW ? fileW.value : "";
  const names = ((data && data.videos) || []).map((v) => v.name);
  setCombo(fileW, names.length ? names : [EMPTY], keep);
  if (!silent) {
    if (data && data.ok) {
      if (!names.length) alert(`目录里没有视频：\n${data.dir || dir}`);
      else console.log(`[QwenVL 视频] ${data.dir} 下找到 ${names.length} 个视频`);
    } else {
      alert(`刷新失败：\n${(data && data.error) || "未知错误"}`);
    }
  }
  grow(node);
}

app.registerExtension({
  name: "QwenVLRemote.VideoLoader",

  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== NODE) return;
    const onNodeCreated = nodeType.prototype.onNodeCreated;

    nodeType.prototype.onNodeCreated = function () {
      const r = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
      const node = this;

      const btn = node.addWidget("button", BTN, null, () => scan(node, { silent: false }));
      if (btn) {
        btn.serialize = false;
        btn.options = btn.options || {};
        const at = node.widgets.findIndex((w) => w.name === DIR_W);
        const cur = node.widgets.indexOf(btn);
        if (cur >= 0) node.widgets.splice(cur, 1);
        node.widgets.splice(at >= 0 ? at + 1 : node.widgets.length, 0, btn);
      }

      const dirW = getWidget(node, DIR_W);
      if (dirW) {
        const origCb = dirW.callback;
        let timer = null;
        dirW.callback = function () {
          const out = origCb ? origCb.apply(this, arguments) : undefined;
          if (timer) clearTimeout(timer);
          timer = setTimeout(() => scan(node, { silent: true }), 900);
          return out;
        };
      }

      // 新加节点时静默扫一次，避免下拉里是空占位
      setTimeout(() => scan(node, { silent: true }), 200);
      grow(node);
      return r;
    };
  },
});
