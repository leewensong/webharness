/* 手机 / 平板的双指控制：左手移动，画布上的另一根手指继续环视或拖动。
   控件只产生归一化输入，不直接改相机；退出/切后台/失焦时立即归零。 */
export function createTouchControls({ hud, t }) {
  const pad = document.createElement("div");
  pad.className = "xr-touch-pad";
  pad.setAttribute("role", "group");
  pad.setAttribute("aria-label", t("xrTouchMove"));
  pad.innerHTML = `<span class="xr-touch-label">${t("xrTouchMove")}</span>
    <div class="xr-touch-stick"><span class="xr-touch-arrows">↑<br>← &nbsp; →<br>↓</span><span class="xr-touch-knob"></span></div>`;
  hud.appendChild(pad);
  const stick = pad.querySelector(".xr-touch-stick");
  const knob = pad.querySelector(".xr-touch-knob");
  const input = { side: 0, forward: 0 };
  let pointerId = null;
  const media = window.matchMedia("(any-pointer: coarse)");
  const reveal = () => hud.classList.add("xr-touch");
  const detect = () => { if (navigator.maxTouchPoints > 0 || media.matches) reveal(); };
  detect();
  media.addEventListener?.("change", detect);

  function reset() {
    const id = pointerId;
    pointerId = null;
    input.side = input.forward = 0;
    knob.style.transform = "translate(0px, 0px)";
    if (id != null) { try { stick.releasePointerCapture(id); } catch {} }
  }
  function update(e) {
    const rect = stick.getBoundingClientRect();
    const radius = rect.width * 0.32;
    let x = (e.clientX - rect.left - rect.width / 2) / radius;
    let y = (e.clientY - rect.top - rect.height / 2) / radius;
    const length = Math.hypot(x, y);
    if (length > 1) { x /= length; y /= length; }
    input.side = Math.abs(x) < 0.12 ? 0 : x;
    input.forward = Math.abs(y) < 0.12 ? 0 : -y;
    knob.style.transform = `translate(${x * radius}px, ${y * radius}px)`;
  }
  function down(e) {
    if (pointerId != null || (e.pointerType === "mouse" && e.button !== 0)) return;
    e.preventDefault();
    e.stopPropagation();
    pointerId = e.pointerId;
    try { stick.setPointerCapture(pointerId); } catch {}
    update(e);
  }
  function move(e) {
    if (e.pointerId !== pointerId) return;
    e.preventDefault();
    e.stopPropagation();
    update(e);
  }
  function end(e) { if (e.pointerId === pointerId) reset(); }
  function onVisibility() { if (document.hidden) reset(); }
  stick.addEventListener("pointerdown", down);
  stick.addEventListener("pointermove", move);
  for (const event of ["pointerup", "pointercancel", "lostpointercapture"]) stick.addEventListener(event, end);
  window.addEventListener("blur", reset);
  document.addEventListener("visibilitychange", onVisibility);
  return {
    input, reset, reveal,
    dispose() {
      reset();
      media.removeEventListener?.("change", detect);
      window.removeEventListener("blur", reset);
      document.removeEventListener("visibilitychange", onVisibility);
      pad.remove();
    },
  };
}
