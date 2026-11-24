// ===========================================================================
//  ComfyUI-QwenVL-Remote / web/dynamic_images.js
//
//  给三个「吃图」的节点加动态图片口：
//      QwenVLRemote / QwenVLRemoteAdvanced / QwenVLRemotePromptEnhancer
//  规则：后端固定声明 图片1…图片10 十个可选 IMAGE 输入；前端只露出
//        「已连线的 + 下面一个空口」，连满一个就自动在下面多长一个，最多 10 个。
//  断开连线会自己收回去，不会留下多余的空口。
// ===========================================================================

import { app } from "../../scripts/app.js";

const NODES = new Set(["QwenVLRemote", "QwenVLRemoteAdvanced", "QwenVLRemotePromptEnhancer"]);
const MAX = 10;
const RE = /^图片(\d+)$/;

function imgInputs(node) {
  return (node.inputs || []).filter((i) => i && RE.test(i.name || ""));
}

function grow(node) {
  try {
    const size = node.computeSize();
    node.setSize([Math.max(node.size[0], size[0]), Math.max(node.size[1], size[1])]);
    node.setDirtyCanvas(true, true);
  } catch (e) {}
}

// 只露 connected+1 个图片口，最多 MAX 个
function refresh(node) {
  if (!node || !node.inputs) return;
  const imgs = imgInputs(node);
  const connected = imgs.filter((i) => i.link != null).length;
  const want = Math.min(MAX, connected + 1);

  // 1) 先收：删掉多出来的空图片口（连着的一律不动）
  for (let guard = 0; guard < MAX + 2; guard++) {
    if (imgInputs(node).length <= want) break;
    const cur = imgInputs(node);
    let removed = false;
    for (let k = cur.length - 1; k >= 0; k--) {
      if (cur[k].link != null) continue;
      const idx = node.inputs.indexOf(cur[k]);
      if (idx < 0) continue;
      node.removeInput(idx);
      removed = true;
      break;
    }
    if (!removed) break;
  }

  // 2) 再长：补到 want 个（名字按顺序接着排）
  let n = imgInputs(node).length;
  while (n < want) {
    let name = null;
    for (let k = 1; k <= MAX; k++) {
      const cand = "图片" + k;
      if (!node.inputs.some((i) => i && i.name === cand)) {
        name = cand;
        break;
      }
    }
    if (!name) break;
    node.addInput(name, "IMAGE");
    n++;
  }

  grow(node);
}

function schedule(node) {
  if (node.__qvrImgTimer) clearTimeout(node.__qvrImgTimer);
  node.__qvrImgTimer = setTimeout(() => {
    node.__qvrImgTimer = null;
    refresh(node);
  }, 30);
}

app.registerExtension({
  name: "QwenVLRemote.DynamicImageInputs",

  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (!NODES.has(nodeData.name)) return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const r = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
      this.__qvrDynImg = true;
      setTimeout(() => refresh(this), 0);
      return r;
    };

    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      const r = onConfigure ? onConfigure.apply(this, arguments) : undefined;
      setTimeout(() => refresh(this), 0);
      return r;
    };

    const onConnectionsChange = nodeType.prototype.onConnectionsChange;
    nodeType.prototype.onConnectionsChange = function (type, slot, connected) {
      const r = onConnectionsChange ? onConnectionsChange.apply(this, arguments) : undefined;
      // type=1 是 INPUT；图片口都在输入侧
      if (type === 1) {
        const inp = this.inputs && this.inputs[slot];
        if (!inp || RE.test(inp.name || "")) schedule(this);
      }
      return r;
    };
  },

  setup() {
    // 打开已存的工作流时也收一遍
    setTimeout(() => {
      for (const node of app.graph ? app.graph._nodes || [] : []) {
        if (node && NODES.has(node.comfyClass) && imgInputs(node).length) refresh(node);
      }
    }, 600);
  },
});
