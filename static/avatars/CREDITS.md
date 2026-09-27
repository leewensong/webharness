# 内置默认 3D 形象 —— 来源与许可

本目录下的 `.vrm` 与 `.webp` 是**第三方素材**，不是本项目原创，用作账号可选的「内置缺省 3D 形象」。

- **注册表**：Open Source Avatars — https://opensourceavatars.com （元数据仓库 `ToxSam/open-source-avatars`）
- **合集**：**100Avatars R1**，共 **100 个**，本目录全部收录。该合集许可证为 **CC0 1.0 Universal（公共领域奉献）**：可自由复制、修改、分发，含商业用途，**无需署名**。
- **本地来源**：`webxr-avatar-demo` 演示项目已缓存好的副本
  - 模型：`dist/models/osa-cc0/<编号>-<名称>.vrm`
  - 头肩像（256×256 WebP）：`dist/chat-avatars/osa-cc0/<编号>-<名称>.webp` —— 由对应 VRM 本地离屏渲染，**不是**注册表官方缩略图

## 本目录是**修改过的副本**

CC0 允许修改。为控制仓库与发布包体积，入库前对每个 `.vrm` 做了两件事：

1. **无损**：删除**没有任何 VRM 表情引用**的 morph target（原始每个模型 16 个靶，实际只用到 6 个）；内容完全相同的靶合并到同一 accessor；随后只把仍被引用的数据重打包。
   经逐字节校验：**6 个表情的形变数据、蒙皮逆矩阵、顶点/法线/UV/骨骼权重/索引全部与原文件完全一致**。
2. **有损（仅贴图）**：贴图最长边降到 512（原为 1024/2048/1080）；带透明通道的仍存 PNG，不透明的改存 JPEG。

结果：100 个模型 **172.6MB → 74.6MB**（约 43%）。

## 逐个对照表

`id` 即 `static/avatars/<id>.vrm` 的文件名（取自注册表 slug）。

| id | 显示名 | 官方原名 | 注册表 avatar id |
|---|---|---|---|
| `devil` | 恶魔 | Devil | `27ccb24c-1fa1-4931-afed-e182b062c950` |
| `polydancer` | 波浪舞者 | Polydancer | `16aaf84a-3b1c-487f-9871-f9daabf4f504` |
| `rose` | 罗丝 | Rose | `8eedc254-88d8-4320-9d96-73acbb7d61cc` |
| `robert` | 罗伯特 | Robert | `56b866fc-c6e8-4635-8abb-b587c4c5b5dd` |
| `bloody` | 血面 | Bloody | `718c61cb-32e7-4292-b44c-0c4f6a903968` |
| `rabbit` | 兔子 | Rabbit | `8df2b84c-a9ad-41f7-b01a-e73fde7db568` |
| `eggplant` | 茄子 | Eggplant | `e82a8a1f-7dda-4fc3-9ff8-6d41e1648300` |
| `bullidan` | 布利丹 | Bullidan | `98b2e431-9953-4ee7-9e0a-5ccc16bd85b6` |
| `mikel` | 米克尔 | Mikel | `3282fb07-86e6-4a10-b95b-bc85d886138c` |
| `coolbanana` | 酷香蕉 | CoolBanana | `c47d2c68-80ae-4131-802a-b1bf12f30398` |
| `skull` | 骷髅 | Skull | `79d66c3e-9bee-4c39-a9d3-aeb96124805c` |
| `observer` | 观察者 | Observer | `a082deff-650e-4f8e-97b1-e0ef305c9228` |
| `nightmare` | 梦魇 | Nightmare | `18db71ff-703c-4ebe-aace-7a57c592ac84` |
| `amazonas` | 亚马逊 | Amazonas | `d5b25951-1564-411f-b441-5b5470687c40` |
| `cookieman` | 饼干人 | Cookieman | `a9556209-f3c4-4c34-909f-39aee67128e4` |
| `dinokid` | 小恐龙 | DinoKid | `f53d6074-f71d-49b9-9f1f-26d7b71c7c9b` |
| `chad` | 查德 | Chad | `bd87c2d4-2dcd-435b-90a7-7617bb20f36d` |
| `clown` | 小丑 | Clown | `3a015e7a-58ff-4bb6-b51e-1dcd5359d392` |
| `chill` | 凉仔 | Chill | `1a26fefc-3e3c-4d37-8b0a-f97260da02a3` |
| `olivia` | 奥莉薇亚 | Olivia | `4239c466-c3c1-4f97-ba56-a871f361ed84` |
| `sticker` | 贴纸 | Sticker | `560dae4f-0a0a-420c-9bb5-759ecd371ccd` |
| `zombie` | 僵尸 | Zombie | `4917f167-2f2e-47c8-8be9-975d206ecd05` |
| `astrodisco` | 迪斯科宇航 | Astrodisco | `2492b104-ec88-4932-9326-2f5a8ad03bc8` |
| `udom` | 乌多姆 | Udom | `66bbb692-ca42-4886-8c3e-a16f0f1d4a3b` |
| `fungus` | 蘑菇精 | Fungus | `dd705f7c-1abe-4f4d-8c8e-7668fe633d40` |
| `coolchoco` | 酷巧克力 | CoolChoco | `a878f2be-2f1b-4ded-9b98-9362f729a41b` |
| `polybot` | 小机器人 | Polybot | `e77c6b30-8cd4-45cc-ba6f-ae4bfb52511e` |
| `ferk` | 叉子 | Ferk | `40cf2651-f725-41a7-ade7-f94aa01aad3e` |
| `erika` | 艾莉卡 | Erika | `f6c50bbf-b41a-470e-84b8-bb3a12efc902` |
| `mummy` | 木乃伊 | Mummy | `88fc50d9-4416-46e0-b839-cfd0a77209b1` |
| `carrot` | 胡萝卜 | Carrot | `bf659356-ff0f-409a-a2cd-a6def10572aa` |
| `lydia` | 莉迪亚 | Lydia | `47309163-deb3-4b83-90b4-5222ac4afbc3` |
| `retroman` | 复古人 | Retroman | `0a4d60d8-a355-4302-9a9a-fa4dff5b8422` |
| `snowy` | 雪人 | Snowy | `3e809bae-2f81-4d4a-ae68-f115933af527` |
| `coffee` | 咖啡杯 | Coffee | `f0d49b06-cdf6-4e09-9057-2c1f77ad56eb` |
| `ro` | 罗 | Ro | `0d5724c3-b7aa-4db0-a235-115eb546dc71` |
| `samuela` | 萨缪拉 | Samuela | `75d554d1-3ea5-4cd0-b4d9-a7580f5f6710` |
| `anchor` | 主播 | Anchor | `1f88bcba-10d0-4b07-8921-f08670982dc8` |
| `teddy` | 泰迪 | Teddy | `72909392-0ce2-429c-bd6f-9405aa992536` |
| `saintclaus` | 圣诞老人 | SaintClaus | `46c3e045-2b63-44e2-87b9-d2d8f4b03560` |
| `milk` | 牛奶盒 | Milk | `15dce553-3d3c-4288-8c03-c69c65167447` |
| `cucumber` | 黄瓜 | Cucumber | `b26d918b-102b-4149-9347-347c0a4a51e5` |
| `astronaut` | 宇航员 | Astronaut | `e69fd8b9-d6ae-44ca-84e0-be4bb075d426` |
| `oldmoustache` | 大胡子 | OldMoustache | `db1c0a1c-b30f-434b-a27e-86ee02764b19` |
| `expol` | 埃克斯波 | Expol | `829a7d51-c90f-4318-8e75-b50cb8a31328` |
| `ghost` | 幽灵 | Ghost | `7c18c121-2e2a-4023-a34d-c01686e11019` |
| `witch` | 女巫 | Witch | `00df253c-4665-4cbf-b420-761a1cc4f9dd` |
| `mafiossini` | 黑手党 | Mafiossini | `0a04d13c-0e04-45ee-98d1-ca6a293e4650` |
| `watermelon` | 西瓜 | Watermelon | `40f83f57-513c-49c1-9681-65c8c7be6cbe` |
| `kate` | 凯特 | Kate | `3d842fb6-29f5-494a-a696-d9ab10a0257d` |
| `coolalien` | 酷外星人 | CoolAlien | `17b46529-4308-493c-a35f-dbf00a771f34` |
| `chilli` | 小辣椒 | Chilli | `c01d25e2-991d-46a2-ae05-82298e900ec4` |
| `toiletpaper` | 卷纸 | ToiletPaper | `4b7a1c36-e76a-47f1-8de7-eb1a5012f6e5` |
| `goodtomato` | 好番茄 | GoodTomato | `6e36690d-c4a4-45d5-83a4-04751e1d4e43` |
| `xmastree` | 圣诞树 | XmasTree | `bdb44645-897f-4b89-9cd9-7c54952c23f0` |
| `wizzir` | 巫师 | Wizzir | `477ff4b9-2d90-4222-bfea-8f512151627c` |
| `skelly` | 骨头人 | Skelly | `ba0cc801-97f8-4c11-8916-144c560f8064` |
| `hotdog` | 热狗 | Hotdog | `9775db0f-67a4-4319-ba59-7dcb4b257f6f` |
| `eyelids` | 眼睑怪 | Eyelids | `5cb67fba-5b27-48e5-b309-afced2f995fb` |
| `froggy` | 青蛙 | Froggy | `ee7f2a28-c2a9-419a-a8e3-84064d48ab16` |
| `baldman` | 光头佬 | Baldman | `6c808fbd-8c20-4cb1-becd-fce97e709fc5` |
| `dracula` | 德古拉 | Dracula | `9207f66b-05e3-4ab6-a0ae-8c38a8c573a5` |
| `shiro` | 小白 | Shiro | `874ac7ff-c2a3-44d5-a9e3-0d02f297d3b0` |
| `pipe` | 烟斗 | Pipe | `d3973be1-5e66-46cb-991f-95ba1f876154` |
| `alwayswatching` | 一直在看 | AlwaysWatching | `ab966521-df9b-4546-a746-c5ad0f67b201` |
| `wolfman` | 狼人 | Wolfman | `a651a392-bf0c-4c5a-9e26-f88abc2b83c3` |
| `angry` | 气鼓鼓 | Angry | `52bc7727-c0a3-4b9f-8792-8ba888e83633` |
| `jennifer` | 珍妮弗 | Jennifer | `bbc3034b-ef3d-414e-8df0-33b81a51fd1c` |
| `muscary` | 肌肉怪 | Muscary | `7e01ceae-7ead-4f3a-a9d1-90b5e23f1c0b` |
| `captainlobster` | 龙虾船长 | CaptainLobster | `fa97c4c8-2845-41f3-bf34-09d9f4dd9dfb` |
| `icecream` | 冰淇淋 | IceCream | `19cc8ab4-81ce-4fcf-9d11-dcd2a0cb2cb4` |
| `cappy` | 帽子客 | Cappy | `a9c61d08-d5f0-4975-964e-68ce6a8592da` |
| `disturbingeyes` | 惊悚之眼 | DisturbingEyes | `4827f43b-7f1b-4961-a65f-76d6cbaba2fc` |
| `aesthetica` | 美学君 | Aesthetica | `4696225a-d6db-4e9b-90a5-ae7fdd607ff1` |
| `lilbro` | 小兄弟 | LilBro | `6bad547c-3840-4ec9-8ecc-605d27eee313` |
| `present` | 礼物盒 | Present | `66f333d3-00ab-4f95-b9d0-06fad2320cf6` |
| `jimmy` | 吉米 | Jimmy | `6aa2b8a8-76a0-4798-9e33-bc7d2a607764` |
| `kyle` | 凯尔 | Kyle | `ee386706-7ff1-4289-82aa-683316b9f574` |
| `pepo` | 佩波 | Pepo | `61fd9426-8533-4f45-85c9-7bc4f49efc8c` |
| `hugo` | 雨果 | Hugo | `93bf02b8-7056-4c7e-8402-ab3b9788492d` |
| `butter` | 黄油块 | Butter | `b2204a07-767a-4e75-99c1-b443b92cdf52` |
| `horrornurse` | 恐怖护士 | HorrorNurse | `2e443c20-48ec-455e-936a-55903e58541e` |
| `scarecrow` | 稻草人 | Scarecrow | `64a9b474-54c4-4a4c-8702-3c8241861fb2` |
| `mushy` | 蘑菇君 | Mushy | `26919c37-74d2-4157-9a9c-c34bee639e21` |
| `bacondude` | 培根哥 | Bacondude | `aeea5263-6db6-4834-a6cb-065d035e8d72` |
| `bigbro` | 大兄弟 | BigBro | `beab28de-739d-4407-8c9c-17633a657cc5` |
| `avocado` | 牛油果 | Avocado | `b4c469c6-07d6-4eba-b887-4396530538c7` |
| `cactusboy` | 仙人掌小子 | CactusBoy | `fe7a9a69-a98a-4c39-8461-ea876d23a260` |
| `david` | 大卫 | David | `540246b8-eba6-4d61-9526-4422df588714` |
| `candycane` | 拐杖糖 | CandyCane | `0cfef0e0-227b-49d2-9b37-2601f23aa955` |
| `franky` | 弗兰奇 | Franky | `8f5f1090-66cf-4aeb-bf24-2a0da3d80676` |
| `wirefriend` | 电线朋友 | WireFriend | `d1ba4803-413f-452e-9d62-1a6e37c59f60` |
| `crimsom` | 克霖森 | Crimsom | `4c3942d1-6b67-434f-889c-138c81f7d79e` |
| `confirmed` | 确认君 | Confirmed | `fe81a7ff-287d-4b97-8e81-3a5a72016294` |
| `wambo` | 万博 | Wambo | `c8ba0374-dc84-4a4e-8a36-1b92f509b4b0` |
| `toothpaste` | 牙膏 | Toothpaste | `4877abd5-b8f5-4f06-a24d-b6006834f330` |
| `weirdflexbutok` | 奇怪炫耀 | WeirdFlexButOk | `d2db8a59-012a-461c-9e45-0db040025ec4` |
| `cubiq` | 立方体 | Cubiq | `44bc039f-2309-4822-bb7e-0d0d88df523d` |
| `mint` | 薄荷 | Mint | `58eea2bf-85db-4e1a-998c-6bf601f40910` |
| `pumpkin` | 南瓜 | Pumpkin | `a07005b6-5df0-4657-b428-2514f175894e` |

## 再引入素材时请注意

注册表 README 明确：**元数据是 CC0，但模型与缩略图各自遵循其所属合集的许可证**；同一演示项目缓存的 800 个模型里**混有 CC-BY 内容**（如 VIPE Heroes，需署名）。扩充本目录时：

1. 逐个确认目标模型所属合集与其许可证；
2. **只用 CC0 合集**（100Avatars R1/R2/R3、Grifters Squaddies、ToxSam、Halloween Rising、Xmas Chibis、NeonGlitch86）才可无署名分发；
3. 引入 CC-BY 内容必须在应用内给出署名，并把署名信息补到本文件。
