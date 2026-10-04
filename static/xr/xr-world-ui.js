/* WebXR 沉浸式世界 UI：把桌面顶部工具栏和底部发消息栏复制成可被
   target-ray/controller 与桌面鼠标拾取的 CanvasTexture 面板。
   DOM overlay 仍保留给系统键盘和无障碍；视觉与按钮在沉浸式里由这里接管。 */
import * as THREE from "three";

const TOP_W = 1800, TOP_H = 170;
const SEND_W = 1500, SEND_H = 170;
const BTN_H = 116;

function roundRect(g, x, y, w, h, r) {
  const rr = Math.min(r, w / 2, h / 2);
  g.beginPath();
  g.moveTo(x + rr, y);
  g.lineTo(x + w - rr, y);
  g.quadraticCurveTo(x + w, y, x + w, y + rr);
  g.lineTo(x + w, y + h - rr);
  g.quadraticCurveTo(x + w, y + h, x + w - rr, y + h);
  g.lineTo(x + rr, y + h);
  g.quadraticCurveTo(x, y + h, x, y + h - rr);
  g.lineTo(x, y + rr);
  g.quadraticCurveTo(x, y, x + rr, y);
  g.closePath();
}

function makePlane(width, height, canvas) {
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.anisotropy = 4;
  const material = new THREE.MeshBasicMaterial({
    map: texture,
    transparent: true,
    depthWrite: false,
    toneMapped: false,
  });
  const mesh = new THREE.Mesh(new THREE.PlaneGeometry(width, height), material);
  mesh.userData.canvas = canvas;
  mesh.userData.ctx = canvas.getContext("2d");
  mesh.userData.texture = texture;
  return mesh;
}

function fitText(g, text, maxWidth) {
  let value = String(text || "");
  if (!value || maxWidth <= 0) return "";
  if (g.measureText(value).width <= maxWidth) return value;
  const suffix = "…";
  while (value.length > 1 && g.measureText(value + suffix).width > maxWidth) value = value.slice(0, -1);
  return value + suffix;
}

export function createXRWorldUI(opts) {
  const t = opts.t || ((key) => key);
  const scene = opts.scene;
  const camera = opts.camera;
  const group = new THREE.Group();
  group.name = "xr-world-ui";
  group.visible = false;

  const topCanvas = document.createElement("canvas");
  topCanvas.width = TOP_W;
  topCanvas.height = TOP_H;
  const sendCanvas = document.createElement("canvas");
  sendCanvas.width = SEND_W;
  sendCanvas.height = SEND_H;
  const top = makePlane(4.15, 0.39, topCanvas);
  const send = makePlane(3.55, 0.39, sendCanvas);
  top.userData.xrWorldUi = "top";
  send.userData.xrWorldUi = "send";
  group.add(top, send);
  scene.add(group);

  const state = {
    immersive: false,
    focusedInput: false,
    topHotspots: [],
    sendHotspots: [],
    anchorPlaced: false,
  };

  function followText() {
    return opts.isFollowing && opts.isFollowing() ? t("xrFollowOn") : t("xrFollowOff");
  }
  function nativeText() {
    return opts.isNative && opts.isNative() ? t("xrNativeOn") : t("xrNativeOff");
  }
  function immersiveMode() {
    return opts.immersiveMode ? opts.immersiveMode() : null;
  }
  function canSwitchImmersive() {
    return !!(opts.canSwitchImmersive && opts.canSwitchImmersive());
  }
  function immersiveSwitchText() {
    return immersiveMode() === "vr" ? t("xrSwitchToAR") : t("xrSwitchToVR");
  }
  function titleText() {
    const room = opts.roomName ? opts.roomName() : "";
    const count = opts.messageCount ? opts.messageCount() : 0;
    return room ? `${room} · ${t("xrMsgCount").replace("{{n}}", String(count))}` : "";
  }

  function hintText() {
    return typeof opts.hint === "function" ? opts.hint() : (opts.hint || "");
  }
  function inputText() {
    const text = opts.inputValue ? String(opts.inputValue() || "") : "";
    return text || (t("xrSendPlaceholder") || "");
  }

  function drawButton(g, r, label, active = false) {
    g.fillStyle = active ? "rgba(91,140,255,0.92)" : "rgba(18,28,44,0.94)";
    roundRect(g, r.x, r.y, r.w, r.h, 28);
    g.fill();
    g.lineWidth = 4;
    g.strokeStyle = active ? "rgba(176,211,255,0.95)" : "rgba(91,140,255,0.72)";
    g.stroke();
    g.fillStyle = "#dfe8f4";
    g.font = "bold 34px system-ui, sans-serif";
    g.textAlign = "center";
    g.textBaseline = "middle";
    g.fillText(label, r.x + r.w / 2, r.y + r.h / 2 + 1);
    g.textAlign = "left";
    g.textBaseline = "alphabetic";
  }

  function drawTop() {
    const { ctx: g } = top.userData;
    g.clearRect(0, 0, TOP_W, TOP_H);
    g.fillStyle = "rgba(7,12,22,0.94)";
    roundRect(g, 2, 2, TOP_W - 4, TOP_H - 4, 32);
    g.fill();
    g.lineWidth = 4;
    g.strokeStyle = "rgba(91,140,255,0.74)";
    g.stroke();
    state.topHotspots = [];
    const y = 27;
    const exit = { x: 24, y, w: 210, h: BTN_H, act: "exit" };
    const follow = { x: 250, y, w: 300, h: BTN_H, act: "follow" };
    const native = { x: 566, y, w: 300, h: BTN_H, act: "native" };
    let nextX = 882;
    const mode = canSwitchImmersive() ? { x: nextX, y, w: 220, h: BTN_H, act: "switchImmersive" } : null;
    if (mode) nextX += mode.w + 16;
    const files = { x: nextX, y, w: 160, h: BTN_H, act: "files" };
    drawButton(g, exit, `‹ ${t("xrExit")}`);
    drawButton(g, follow, followText(), !!(opts.isFollowing && opts.isFollowing()));
    drawButton(g, native, nativeText(), !!(opts.isNative && opts.isNative()));
    if (mode) drawButton(g, mode, immersiveSwitchText());
    drawButton(g, files, t("xrFiles"));
    state.topHotspots.push(exit, follow, native);
    if (mode) state.topHotspots.push(mode);
    state.topHotspots.push(files);
    g.fillStyle = "#8fa3bd";
    g.font = "bold 30px system-ui, sans-serif";
    g.textBaseline = "middle";
    const rightX = files.x + files.w + 34;
    const rightW = TOP_W - rightX - 24;
    g.fillText(fitText(g, titleText(), rightW), rightX, 58);
    g.fillStyle = "#7186a3";
    g.font = "24px system-ui, sans-serif";
    g.fillText(fitText(g, hintText(), rightW), rightX, 116);
    g.textBaseline = "alphabetic";
    top.userData.texture.needsUpdate = true;
  }

  function drawSend() {
    const { ctx: g } = send.userData;
    g.clearRect(0, 0, SEND_W, SEND_H);
    g.fillStyle = "rgba(7,12,22,0.94)";
    roundRect(g, 2, 2, SEND_W - 4, SEND_H - 4, 32);
    g.fill();
    g.lineWidth = 4;
    g.strokeStyle = "rgba(91,140,255,0.74)";
    g.stroke();
    state.sendHotspots = [];
    const input = { x: 24, y: 25, w: 970, h: BTN_H, act: "input" };
    const mic = { x: 1012, y: 25, w: 205, h: BTN_H, act: "mic" };
    const sendBtn = { x: 1235, y: 25, w: 240, h: BTN_H, act: "send" };
    g.fillStyle = state.focusedInput ? "rgba(27,47,78,0.98)" : "rgba(18,28,44,0.94)";
    roundRect(g, input.x, input.y, input.w, input.h, 42);
    g.fill();
    g.lineWidth = 4;
    g.strokeStyle = state.focusedInput ? "rgba(91,140,255,1)" : "rgba(91,140,255,0.72)";
    g.stroke();
    let value = inputText();
    g.font = "35px system-ui, sans-serif";
    while (value.length > 2 && g.measureText(value).width > input.w - 56) value = "…" + value.slice(2);
    g.fillStyle = opts.inputValue && opts.inputValue() ? "#dfe8f4" : "#7c90aa";
    g.textBaseline = "middle";
    g.fillText(value, input.x + 28, input.y + input.h / 2 + 1);
    g.textBaseline = "alphabetic";
    drawButton(g, mic, opts.isAsrOn && opts.isAsrOn() ? t("xrMicStop") : t("xrMic"), !!(opts.isAsrOn && opts.isAsrOn()));
    drawButton(g, sendBtn, t("xrSend"));
    state.sendHotspots.push(input, mic, sendBtn);
    send.userData.texture.needsUpdate = true;
  }

  function refresh() {
    drawTop();
    drawSend();
  }

  function placeAtRoom() {
    /* 与消息墙同一局部坐标系：顶部在消息带上方，输入栏在消息带下方。
       UI 不跟随头部移动，沉浸式里才真正像房间里的控制台。 */
    const centerX = (opts.logLeft == null ? -0.7 : opts.logLeft) + (opts.logWidth == null ? 1.9 : opts.logWidth) / 2;
    const z = (opts.logZ == null ? -6 : opts.logZ) + (opts.frontOffset == null ? 0.36 : opts.frontOffset);
    top.position.set(centerX, (opts.bandTop == null ? 2.42 : opts.bandTop) + 0.42, z);
    send.position.set(centerX, Math.max(0.18, (opts.floorY == null ? 0.42 : opts.floorY) - 0.28), z + 0.02);
    top.rotation.set(0, 0, 0);
    send.rotation.set(0, 0, 0);
    state.anchorPlaced = true;
  }

  function setVisible(visible) {
    state.immersive = !!visible;
    group.visible = !!visible;
    if (!visible) state.focusedInput = false;
    if (visible && !state.anchorPlaced) placeAtRoom();
    group.updateMatrixWorld(true);
    if (visible) refresh();
  }

  function setFocusedInput(focused) {
    state.focusedInput = !!focused;
    if (state.immersive) drawSend();
  }

  function meshHit(ray) {
    if (!group.visible) return null;
    const hits = ray.intersectObjects([top, send], false);
    if (!hits.length) return null;
    const mesh = hits[0].object;
    const uv = hits[0].uv;
    const w = mesh === top ? TOP_W : SEND_W;
    const h = mesh === top ? TOP_H : SEND_H;
    return { mesh, px: uv.x * w, py: (1 - uv.y) * h };
  }

  function handlePick(ray) {
    const hit = meshHit(ray);
    if (!hit) return false;
    const list = hit.mesh === top ? state.topHotspots : state.sendHotspots;
    const spot = list.find((r) => hit.px >= r.x && hit.px <= r.x + r.w && hit.py >= r.y && hit.py <= r.y + r.h);
    if (!spot) return true;
    if (spot.act === "exit") opts.onExit?.();
    else if (spot.act === "follow") opts.onFollow?.();
    else if (spot.act === "native") opts.onNative?.();
    else if (spot.act === "switchImmersive") opts.onSwitchImmersive?.();
    else if (spot.act === "files") opts.onFiles?.();
    else if (spot.act === "input") {
      state.focusedInput = true;
      opts.focusInput?.();
      refresh();
    } else if (spot.act === "mic") opts.onMic?.();
    else if (spot.act === "send") opts.onSend?.();
    return true;
  }

  function dispose() {
    group.remove(top, send);
    top.geometry.dispose();
    send.geometry.dispose();
    top.userData.texture.dispose();
    send.userData.texture.dispose();
    top.material.dispose();
    send.material.dispose();
    if (group.parent) group.parent.remove(group);
  }

  return {
    group,
    top,
    send,
    refresh,
    setVisible,
    setFocusedInput,
    handlePick,
    placeAtRoom,
    dispose,
    state,
  };
}
