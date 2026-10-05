# XR 手掌朝向与 VRM 骨骼坐标转换

## 问题与约定

`gripSpace`、手势 `wrist` 和 VRM 的 `Hand` 骨骼不是同一套轴。
直接将 XR 世界四元数写给 Hand 骨骼，会让平掌看起来朝下/侧面；简单地在世界 X 上
加减 90° 也不对，转身以及左/右手会出现不同结果。

WebXR 官方约定（以下是概括，不是摘录）：

- 手势 joint/wrist：`-Z` 沿骨骼从腕部指向手指，`-Y` 是从手心向外的法线；wrist 的 `-Z` 指向手掌中心。
- 手柄 grip：`-Z` 沿假想握住的杆指向拇指；右手手背朝 `+X`，左手手背朝 `-X`；`+Y` 大致向手臂方向。
- 射线 `targetRaySpace` 是指向用途，不能当作精确的手心朝向。

出处：
- https://immersive-web.github.io/webxr/#dom-xrinputsource-gripspace
- https://immersive-web.github.io/webxr-hand-input/#xrjointspace
- https://github.com/pixiv/three-vrm/blob/dev/packages/three-vrm-core/src/humanoid/VRMHumanoidRig.ts

## 统一网络坐标系 palm-v1

保持现有 presence 二进制 v3，不增加字段宽度。采集端将手柄 grip 四元数转换为手掌
语义四元数；wrist 已经符合该坐标系，不做多余的 90° 旋转。

```json
{
  "p": [0, 1.7, 0],
  "hands": [{"handedness": "right", "p": [0.3, 1.2, -0.4], "q": [0, 0, 0, 1]}],
  "state": {"handOrientation": "palm-v1"}
}
```

`palm-v1` 四元数的局部 `-Z` 指向手指，局部 `-Y` 指向手心外侧。
所有 q 均为世界旋转，包含 rig 的平移/吸附转身。

新客户端的 state 标记防止接收端二次 grip 校正。没有标记的旧数据暂按 grip 处理；
旧手势客户端无法与 grip 区分，**需要头显采集端和观看端都刷新**才能获得确定的结果。
采集端另带递增的 `state.handSample`，让标记在后续采样中重复下发，防止切换模式的
第一包被 presence 合并窗口吞掉，也能让重新进入近距离 LOD 的观看者收到该约定。
Agent 的直接 `bones` / `animation` 不改语义，也不应用人类 IK 转换。

## 模型手骨转换

渲染端加载时，从 normalized rig 的手腕、中指/食指/小指掌指关节测量：

1. 腕 → 掌指关节的手指方向。
2. 食指 → 小指的掌面横向关系（左/右手分别处理）。
3. 正交化得到手心法线和手掌基坐标系。

右乘该骨骼局部基转换，将 `palm-v1` 世界朝向变成 normalized 手骨的世界朝向；
再乘父骨骼世界旋转的逆，得到该手骨局部四元数；最后经 `vrm.update()` 重定向到
实际网格骨骼。模型缺少手指骨骼时回退到 normalized VRM T-pose（手心向下）。

实现位于 `static/xr/xr-hand-pose.js`、`static/xr/xr-avatars.js`、`static/xr/xr-main.js`。
这是掌面方向映射，不包含手指关节弯曲追踪；grip 代表握持帧，不是伸开的手指实测方向。
厂商人体工学/握持习惯仍可能需要独立 profile 校准，但不应混进通用骨骼坐标转换。

## 回归验证

`scripts/unit_xr_animation.mjs` 不只断言“IK active”，而是计算原始 mesh rig 的
手指方向与手心法线，与目标方向的 dot 比较（>0.99）。覆盖：

- Rabbit、Witch、Astrodisco、Polybot 四个真实 VRM0 模型；
- 基于真实 Rabbit 网格构造的 VRM1 声明，验证另一条 humanoid retargeting 路径；
- 左/右手，平掌、翻掌、竖掌、复合旋转和等价 q/-q；
- 不同身体 yaw、非原点站位；
- 手柄 grip 与 wrist 的坐标转换，输入模式切换与旧 grip 数据；
- 断连/遮挡不会发出幽灵手或使用过期 wrist。

`tests/test_presence_animation.py` 验证 palm-v1 标记通过现有 state 编解码保持不丢。
上述自动化使用合成 XR 空间；真正 Quest 的握持体验仍需用户上机验收，不能用
自动化代替硬件实测。
