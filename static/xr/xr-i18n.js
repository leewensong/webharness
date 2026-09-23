/* WebXR 3D 渲染端补充文案（zh/en）。3D 模块按需动态加载，加载时把这里的键合并进
   2D 端的 I18N 字典（const 字典允许新增属性）；顶栏「3D」按钮等首屏相关键
   直接放在 index.html 的字典里，不在此处，避免 2D 首屏依赖本文件。 */

export const XR_I18N = {
  zh: {
    xrHintDesktop: "拖拽环视 · 滚轮走近 · W/S 移动 · 拖右侧滚行条或 ←/→ 翻阅历史 · F 跟随最新 · Esc 返回 2D",
    xrMsgCount: "共 {{n}} 条",
    xrFollowOn: "跟随最新：开",
    xrFollowOff: "跟随最新：关",
    xrBackfilling: "正在加载更早的消息…",
    xrHistoryEnd: "已到最早一条消息",
    xrRoomEmpty: "这个房间还没有消息",
    xrBackfillFail: "历史回填失败，可稍后滚动重试",
    xrSegmentHint: "内容较长 · 点击面板继续阅读",
    xrPanelDegraded: "（该消息栅格化失败，已降级为文本）",
    xrModelLoading: "正在放置模型…",
    xrModelLoadFail: "模型加载失败（格式或网络问题），已跳过",
    xrNativeOn: "原生图表：开",
    xrNativeOff: "原生图表：关（面板模式兜底）",
    xrOwnerTag: "房主",
    xrEnterVR: "进入头显",
    xrExitVR: "退出头显",
    xrVRFail: "无法进入头显（需要 WebXR 设备与授权）",
    xrMic: "语音",
    xrMicStop: "停止",
    xrVrVoice: "录音",
    xrRecording: "录音中",
    xrVoiceSent: "语音已发送",
    xrVoiceUnsupported: "当前环境不支持发送语音消息",
    xrVRHint: "按住手柄侧握键说话 · 右摇杆上下翻历史 · 拖记录或右侧滚行条也能翻 · 脚边控制条可返回",
    xrAsrUnsupported: "当前浏览器不支持语音识别",
    xrSend: "发送",
    xrSendPlaceholder: "发消息（复杂编辑请回 2D）…",
    xrSendFail: "发送失败，请重试",
  },
  en: {
    xrHintDesktop: "Drag to look around · wheel to move · W/S walk · drag the scrollbar or ←/→ to browse history · F follow latest · Esc back to 2D",
    xrMsgCount: "{{n}} messages",
    xrFollowOn: "Follow latest: on",
    xrFollowOff: "Follow latest: off",
    xrBackfilling: "Loading older messages…",
    xrHistoryEnd: "Reached the oldest message",
    xrRoomEmpty: "No messages in this room yet",
    xrBackfillFail: "History backfill failed, scroll to retry",
    xrSegmentHint: "Long content · click panel to continue",
    xrPanelDegraded: "(rasterization failed, degraded to plain text)",
    xrModelLoading: "Placing model…",
    xrModelLoadFail: "Model failed to load (format or network); skipped",
    xrNativeOn: "Native charts: on",
    xrNativeOff: "Native charts: off (panel fallback)",
    xrOwnerTag: "Owner",
    xrEnterVR: "Enter VR",
    xrExitVR: "Exit VR",
    xrVRFail: "Could not enter VR (WebXR device and permission required)",
    xrMic: "Voice",
    xrMicStop: "Stop",
    xrVrVoice: "Record",
    xrRecording: "Recording",
    xrVoiceSent: "Voice message sent",
    xrVoiceUnsupported: "Voice messages are not supported in this environment",
    xrVRHint: "Hold the grip button to talk · right stick up/down or drag the log/scrollbar to browse history · bar at your feet to return",
    xrAsrUnsupported: "Speech recognition is not supported by this browser",
    xrSend: "Send",
    xrSendPlaceholder: "Send a message (complex editing in 2D)…",
    xrSendFail: "Send failed, try again",
  },
};

export function mergeXRI18n(I18N) {
  if (!I18N) return;
  for (const lang of Object.keys(XR_I18N)) {
    if (I18N[lang]) Object.assign(I18N[lang], XR_I18N[lang]);
  }
}