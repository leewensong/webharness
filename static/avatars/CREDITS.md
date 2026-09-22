# 内置默认 3D 形象 —— 来源与许可

本目录下的 `.vrm` 与 `.webp` 是**第三方素材**，不是本项目原创。它们被用作账号可选的「内置缺省 3D 形象」。

- **注册表**：Open Source Avatars — https://opensourceavatars.com （元数据仓库 `ToxSam/open-source-avatars`）
- **合集**：**100Avatars R1**（该合集自身的许可证为 **CC0 1.0 Universal / 公共领域奉献**，可自由复制、修改、分发，含商业用途，无需署名）
- **本地来源**：`webxr-avatar-demo` 演示项目已缓存好的副本
  - 模型：`dist/models/osa-cc0/<编号>-<名称>.vrm`
  - 头肩像（256×256 WebP）：`dist/chat-avatars/osa-cc0/<编号>-<名称>.webp`
- **`.webp` 的性质**：由对应的 VRM 本地离屏渲染出的头肩像，用于选择器卡片，**不是**注册表官方的展示缩略图。

| 本项目 id | 原名 | 注册表描述 | 注册表 avatar id | 原始 `model_file_url` |
|---|---|---|---|---|
| `robert` | Robert | Avatar 070: Robert | `56b866fc-c6e8-4635-8abb-b587c4c5b5dd` | `https://arweave.net/gwG7w4bY-A5c3R6A6GOz3xBCgbPvkFQmqPIDtvnNsYI` |
| `erika` | Erika | Avatar 053: Erika | `f6c50bbf-b41a-470e-84b8-bb3a12efc902` | `https://arweave.net/GZkfa0SNnrBWluRL_pXpakg7T3K3d4l87__wR4mD3UM` |
| `david` | David | Avatar 047: David | `540246b8-eba6-4d61-9526-4422df588714` | `https://arweave.net/H3cBhsOEoiQ8XZiwG31SyCUtiDewBZRccxIDztyHfSY` |
| `astronaut` | Astronaut | Avatar 048: Astronaut | `e69fd8b9-d6ae-44ca-84e0-be4bb075d426` | `https://arweave.net/T0c0z_XEPQHy3vyXz31XB22s_6JTqHdnau8exq_I8tI` |
| `polybot` | Polybot | Avatar 051: Polybot | `e77c6b30-8cd4-45cc-ba6f-ae4bfb52511e` | `https://arweave.net/DUR8v-IugXppdMBxPdE1rDO2dZCJJ7ZgBTXSRgPJFNo` |
| `ghost` | Ghost | Avatar 034: Ghost | `7c18c121-2e2a-4023-a34d-c01686e11019` | `https://arweave.net/fSy4hx9L9SqiQIKzjhRLhXzDZpQEJA5izCcDej_WJi8` |

文件名中的编号是**注册表中该合集数组的 1-based 序号**（与注册表条目里的 `metadata.number` 是另一套编号，两者不同）；本项目改用语义化的 id（如 `robert`），对照关系见上表。

模型均为 **VRM 0.x**，各含 6 个表情预设（a / i / u / e / o / blink）与人形骨骼（`polybot` 为 31 根，其余 52 根）。

## 再引入素材时请注意

注册表 README 明确：**元数据是 CC0，但模型与缩略图各自遵循其所属合集的许可证**。同一演示项目缓存的 800 个模型里**混有 CC-BY 内容**（如 VIPE Heroes，需署名）。若要扩充本目录：

1. 逐个确认目标模型所属合集与其许可证；
2. **只用 CC0 合集**（100Avatars R1/R2/R3、Grifters Squaddies、ToxSam、Halloween Rising、Xmas Chibis、NeonGlitch86）才可无署名分发；
3. 引入 CC-BY 内容必须在应用内给出署名，并把署名信息补到本文件。
