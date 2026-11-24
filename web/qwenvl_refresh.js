// ===========================================================================
//  ComfyUI-QwenVL-Remote / web/refresh.js
//
//  只服务一个节点：QwenVLRemoteLoader（QwenVL 远程加载器）。给它加：
//    · 「🔄 刷新模型列表」整宽按钮 —— 从服务端拉 模型 + 视觉模型 + 显卡型号
//    · 「服务端显卡」只读栏 —— 值由按钮/自动探测写入，手改会被还原
//    · 「🗑 卸载服务端模型」按钮 —— 让服务端把 LLM 退出显存
//  浏览器跨域会被挡，所以走本机代理 /qwenvl_remote/models（见 api.py）。
// ===========================================================================

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE = "QwenVLRemoteLoader";
const URL_W = "服务端地址";
const GPU_W = "服务端显卡";
const MODEL_W = "模型";
const VISION_W = "视觉模型";
const BTN = "🔄 刷新模型列表";
const UNLOAD_BTN = "🗑 卸载服务端模型";

const NO_VISION = "（不使用 / 纯文本）";
const FAIL_TEXT = "（连接失败，检查服务端地址）";
const EMPTY_TEXT = "（服务端没有可用模型）";

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

function setComboValues(widget, values, keep) {
  if (!widget) return;
  if (!widget.options) widget.options = {};
  widget.options.values = values;
  const cur = keep !== undefined ? keep : widget.value;
  widget.value = values.includes(cur) ? cur : values.length ? values[0] : "";
}

function setText(widget, text) {
  if (!widget || widget.value === text) return;
  widget.value = text;
  if (widget.inputEl) widget.inputEl.value = text;
}

// 「服务端显卡」只读：值由节点写入，手改立刻还原
function lockGpuWidget(node, name) {
  const w = getWidget(node, name);
  if (!w) return null;
  const auto = { value: w.value || "" };
  const origCb = w.callback;
  w.callback = function () {
    const out = origCb ? origCb.apply(this, arguments) : undefined;
    if (this.value !== auto.value) {
      this.value = auto.value;
      node.setDirtyCanvas(true, true);
    }
    return out;
  };
  if (w.inputEl) {
    w.inputEl.readOnly = true;
    w.inputEl.style.opacity = "0.7";
  }
  return (text) => {
    auto.value = text || "";
    setText(w, text || "");
    node.setDirtyCanvas(true, true);
  };
}

function serverURL(node) {
  const w = getWidget(node, URL_W);
  return ((w && w.value) || "").trim();
}

async function fetchLists(url) {
  try {
    const resp = await api.fetchApi(`/qwenvl_remote/models?url=${encodeURIComponent(url)}`, {
      cache: "no-store",
    });
    return await resp.json();
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e), llm: [], llm_vision: [], device: "" };
  }
}

async function probe(node, { silent = true } = {}) {
  const url = serverURL(node);
  const setGpu = node.__qvrSetGpu;
  if (!url) {
    if (setGpu) setGpu("（先填服务端地址）");
    return;
  }
  const data = await fetchLists(url);
  const gpuW = getWidget(node, GPU_W);
  const modelW = getWidget(node, MODEL_W);
  const visionW = getWidget(node, VISION_W);
  const keepModel = modelW ? modelW.value : "";
  const keepVision = visionW ? visionW.value : NO_VISION;

  if (data && data.ok) {
    if (setGpu) setGpu(data.device || "（服务端没报显卡型号）");
    const llm = (data.llm || []).slice();
    const vis = (data.llm_vision || []).slice();
    setComboValues(modelW, llm.length ? llm : [EMPTY_TEXT], keepModel);
    setComboValues(visionW, [NO_VISION].concat(vis), keepVision);
    if (!silent) {
      if (!llm.length) alert("服务端没有提示词模型：\n" + (data.error || ""));
      else console.log(`[QwenVL 远程] 已刷新 ${llm.length} 个模型、${vis.length} 个视觉模型`);
    }
  } else {
    if (setGpu) setGpu("（连不上服务端）");
    setComboValues(modelW, [FAIL_TEXT], keepModel);
    setComboValues(visionW, [NO_VISION], keepVision);
    if (!silent) alert("刷新失败：\n" + ((data && data.error) || ""));
  }
  grow(node);
}

async function doUnload(node) {
  const url = serverURL(node);
  if (!url) {
    alert("先填「服务端地址」");
    return;
  }
  let data = { ok: false, error: "无响应" };
  try {
    const resp = await api.fetchApi(`/qwenvl_remote/unload?url=${encodeURIComponent(url)}`, {
      cache: "no-store",
    });
    data = await resp.json();
  } catch (e) {
    data = { ok: false, error: String((e && e.message) || e) };
  }
  alert(data && data.ok ? "已让服务端卸载 QwenVL 模型（显存已还回去）" : "卸载失败：\n" + ((data && data.error) || ""));
}

app.registerExtension({
  name: "QwenVLRemote.RefreshModels",

  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== NODE) return;
    const onNodeCreated = nodeType.prototype.onNodeCreated;

    nodeType.prototype.onNodeCreated = function () {
      const r = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
      const node = this;

      node.__qvrSetGpu = lockGpuWidget(node, GPU_W) || node.__qvrSetGpu;

      // 刷新按钮紧跟在「服务端地址」下面
      const btn = node.addWidget("button", BTN, null, () => probe(node, { silent: false }));
      if (btn) {
        btn.serialize = false;
        btn.options = btn.options || {};
        const cur = node.widgets.indexOf(btn);
        if (cur >= 0) node.widgets.splice(cur, 1);
        const at = node.widgets.findIndex((w) => w.name === URL_W);
        node.widgets.splice(at >= 0 ? at + 1 : node.widgets.length, 0, btn);
      }

      // 卸载按钮放最后
      const ub = node.addWidget("button", UNLOAD_BTN, null, () => doUnload(node));
      if (ub) {
        ub.serialize = false;
        ub.options = ub.options || {};
      }

      // 改地址后自动重探（静默）
      const urlW = getWidget(node, URL_W);
      if (urlW) {
        const origCb = urlW.callback;
        let timer = null;
        urlW.callback = function () {
          const out = origCb ? origCb.apply(this, arguments) : undefined;
          if (timer) clearTimeout(timer);
          timer = setTimeout(() => probe(node, { silent: true }), 900);
          return out;
        };
      }

      // 打开工作流 / 新建节点后先静默拉一次
      setTimeout(() => probe(node, { silent: true }), 400);
      grow(node);
      return r;
    };
  },
});
