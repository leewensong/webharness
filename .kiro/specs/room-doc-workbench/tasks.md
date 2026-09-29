# 实施计划 — 房间共创工作台（Room Doc Workbench）

> 新会话从这里开始。**按里程碑推进，每个里程碑独立可发布**（各自递增小版本：M1 → 2.22.0、M2 → 2.23.0、M3 → 2.24.0…）。仓库当前版本 2.21.x，2.18 的「共同文件」已交付并上线（语义见 `.kiro/specs/room-shared-files/`）。
>
> **前置事实（新会话必读）**：既有 `kind` 8 类、LWW、revision 长轮询、`baseUpdatedAt`、`canEditFiles`/`filesLocked`、`world_visible/world_pose` 摆入契约、`xr-files.js` 单 uiMesh 双 CanvasTexture 面板，**全部沿用不改**。本规格全部为**新增**。
>
> 每个里程碑结束的验证手段：`e2e.sh` + 2D 浏览器实测 + 桌面 3D 预览（`?xr=1` 或 Quest）。
>
> **已知失败项**：`e2e.sh` 固定有 1 项陈旧断言「超长 422」，不是回归。

---

## 里程碑 M1：类型体系骨架 + 五个低成本高价值类型（目标 2.22.0）

> 目标：把「类型」从三端硬编码升级为**注册表驱动**，并立刻产出「房间里能画数据图」的效果。**不含** patch / 批注 / 转换（属 M2+）。

- [ ] 1. 服务端类型注册表
  - `app/doc_types.py`（新建）：`DOC_TYPES` 字典（结构见 design.md 三节），先落 **13 条**：既有 8 类语义映射（`markdown`/`text`/`svg`/`image`/`audio`/`video`/`model`/`other`）+ 新增 5 条（`chart`、`chart3d`、`csv`、`mermaid`、`markmap`）
  - `classify(name, declared_mime, data, hint=None) -> (doc_type, kind, mime)`：**复用 `_classify_file` 的同一次字节嗅探**，先生成 kind，再在注册表内按 ext 命中细粒度 doc_type；`hint`（来自 `?as=`）仅在 `accepts` 集合内生效，否则 400
  - `capabilities(doc_type) -> dict`：能力位快照（`textEditable/agentFriendly/web/xr/placement/cost/security/deps`），不含 `template`/`docs`（给单文件响应瘦身）
  - `GET /api/doc-types`：返回全表（含 `template` 与 `docs`），成员鉴权即可（无需管理员）
  - _需求：1.1–1.7_

- [ ] 2. 数据层（只增）
  - `app/db.py`：`_add_column_if_missing("room_files", "doc_type", "TEXT")`；**不建索引、不写迁移脚本**
  - `_room_files_dict`：`docType` 取值 = `row.doc_type or classify(name)`（**惰性推导**，读时不写库）；新增 `capabilities` 块
  - 写入路径（创建/替换/改名）补齐 `doc_type` 落库
  - _需求：1.3、10.1、10.2_

- [ ] 3. 客户端渲染器插件机制 + **预览三型**
  - `static/doc-renderers/_registry.js`（新建）：`renderInto(el, file, content)` —— 先按 `file.docType` 查插件（`dynamic import()` + 内存缓存），失败回落 `kind` 渲染（把现有 `index.html` 里的 kind 预览矩阵抽出为 `_builtin.js`），再回落信息卡
  - **三型判定函数**（纯函数，唯一实现，2D 与 XR 共用）：`previewTier(docType) → P1 | P2 | P3`，规则 `有 3D 渲染器 → P1；否则有 2D 渲染器 → P2；否则 → P3`。**SHALL NOT 写进数据**——渲染器上线即自动升级全部历史文件
  - 插件统一接口：2D `{canRender, render(el, content, ctx)}`；XR `{toTexture(content), toEntity(THREE, content, ctx), measure(obj)}`；`live: true` 者声明为 P2b 活纹理
  - **P3 缺省几何体**（新建 `static/doc-renderers/_placeholder.js`）：薄板（**给厚度或 DoubleSide，别用零厚平面**）+ 文件名纹理（canvas 画字，不依赖字体文件）+ 按 doc_type/扩展名**稳定哈希 → 色相**（同类型同色、不同类型不同色）+ 扩展名角标；命中显示信息卡（大小/更新者/下载/删除），**不做任何内容解析、不生成缩略图、不尝试渲染**（P3 = 有位置、无预览）。**注意：P3 若无区分度，房间里 5 个未知文件会变成一堆一样的白盒子**
  - **P2 图源只走客户端**（已拍板）：① 原图直用 ② 复用 2D 渲染代码栅格化；**不引入服务端预生成**（服务器保持「透传与最小校验」）
  - `static/index.html`：文件抽屉预览改为调用 `renderInto`；删除内联的 kind 分支（**行为等价迁移**，先不新增类型以外的东西）
  - _需求：3.1、3.3、4.1、4.2、8.1、8.4_

- [ ] 4. 新增类型 `chart`（ECharts option JSON）
  - 插件 `chart.js`：`render` 调 `window.echarts`（vendor 已有）；option 解析失败 → 降级源码视图
  - `template`：一份柱状图骨架；`validate` 不做（宽松）
  - _需求：2.5、3.2_

- [ ] 5. 新增类型 `csv`（表格）
  - 插件 `csv.js`：自写 RFC 4180 解析（含引号转义），渲染为 sticky 表头表格；>2000 行虚拟滚动或分页
  - 识别：`.csv`/`.tsv`，mime `text/csv`；`limits.text` 保持 2MB
  - _需求：3.2、2.2_

- [ ] 6. 新增类型 `mermaid`（从 text 中独立）
  - 识别：`.mermaid`/`.mmd` 与 `text` 内含 mermaid 的内容（保持既有行为不破坏）；插件 `mermaid.js` 复用既有 `mermaid.run` 管线
  - 既有 `kind=text` 且扩展名 `.mermaid/.mmd` 的文件，`docType` 惰性推导为 `mermaid`（**列表显示不变**，只是多了能力位）
  - _需求：1.6、4.3_

- [ ] 7. 新增类型 `markmap`（Markdown → 思维导图）
  - 引入 `static/vendor/markmap.min.js`（或 markmap-lib + markmap-view）；插件 `markmap.js`
  - 识别：`.markmap.md` 或 `?as=markmap`；`template`：两级大纲骨架
  - _需求：3.2、2.5_

- [ ] 8. **新增类型 `chart3d`（XR 旗舰）**
  - 插件 `chart3d.js`：内容 schema `{type: "bars"|"scatter3d"|"surface", axes:{x,y,z}, series:[…], style:{…}}`
  - `render`（2D）：three 离屏渲染一帧 → 缩略图
  - `toEntity(THREE, content)`：生成 3D 柱体/散点/曲面 `Object3D`（柱体用 `InstancedMesh`，>1000 实例抽稀）
  - `measure`：包围盒归一化（复用 2.18 首次摆放的 `1/Box3.maxDim` 约定）
  - 服务端 `validate`：顶层 schema 校验，失败 400 并附定位（**这是本规格唯一做内容校验的类型**）
  - `template`：三维柱状图骨架（Agent 可直接生成）
  - _需求：2.4、2.5、4.1、4.2_

- [ ] 9. 摆入泛化（三型统一 + 预算制）
  - `PUT .../files/{id}/placement`：放宽「仅 kind=model」→ 改为「三型都可摆入」（`mount` 为 `none` 者除外，罕见）；其余校验不变
  - `world` 块新增 `mount: "entity" | "panel" | "placeholder" | "player"`（既有 model 返回 `entity`，**旧客户端忽略该字段**）
  - 服务端预算：`entity ≤ 8`、`panel ≤ 8`、`Σcost ≤ 12`（cost 取自注册表：GLB=1、chart3d=2、ifc=3、**P3 占位体=2**）；既有 model=1，天然满足
  - `xr-files.js`：面板式摆入 —— 把插件 `toTexture()` 的大画布作为 `CanvasTexture` 挂到 panel mesh（复用既有 uiMesh/纹理管线）；`placeholder` 走 P3 缺省几何体；adjBar 逻辑对三型一致
  - **纹理按需生成**：仅摆入者生成纹理；分辨率按距离/聚焦分级；P2b 活纹理 ≤6 张同时活跃，其余降静态小图
  - _需求：4.2–4.8_

- [ ] 10. 能力位在 2D/XR 的呈现
  - 2D 文件抽屉：每种类型显示类型徽标（可编辑/仅预览/可摆入），「摆入房间」入口按 `placement` 显隐
  - XR 文件面板：种类图标扩展为注册表驱动（新增 5 个图标）
  - `static/xr/xr-i18n.js` + `index.html`：新增文案 zh/en
  - _需求：3.6、4.5、10.4_

- [ ] 11. 文档与发布
  - `/skill.md` 新增「文档类型与共创工作台」章节：`GET /api/doc-types` 用法、5 个新类型的示例与 template、内容路由补充（数据图 → `chart`，三维数据 → `chart3d`）
  - `e2e.sh`：doc type 识别、`?as=` 越权 400、`/api/doc-types` 200、chart3d 摆入实体 → 返回 `mount:entity`、panel 摆入、预算超限 400、非可摆入类型 400、旧客户端按 kind 降级（列表字段不变）
  - 版本 2.22.0；README/`app/main.py` description 同步
  - _需求：10.3、10.5、10.6_

---

## 里程碑 M2：共创闭环 + 模型/地图族 + 演示厅（目标 2.23.0）

> 目标：让「人类与 Agent 同时改」真正成立（patch + 批注），并把 XR 3D 主场铺开（geo3d、obj/stl、deck、空间音频）。

- [ ] 12. 局部修改协议 `PATCH .../files/{id}/content`
  - 三种 mode：`jsonPointer`（JSON/YAML→JSON 视图，`ops:[{op,path,value?}]`，自写轻量 pointer 实现，**不引 jsonpatch 依赖**）、`anchor`（Markdown 标题/`<!-- anchor: x -->`）、`range`（`startLine/endLine` 或 `offset/length`）、`append`（range 特例）
  - 原子读-改-写；成功 `updated_at` 递增 + revision+1 + `notify_room`；**不存历史版本**
  - 锚点不存在 → 422 + 可用锚点列表（从当前内容提取标题）
  - _需求：5.1、5.2、5.3、5.5_

- [ ] 13. 三方合并（`baseUpdatedAt` 语义升级）
  - 客户端提交 `baseContent`（或服务端以 `baseUpdatedAt` 无法取回 base 内容时退化为 **远端优先 + 409**）；有 base 时按「远端改动区间 ∩ 本地改动区间」判冲突
  - `jsonPointer`：按路径集合判定，不同路径各自应用（`merged:true`）
  - `range/anchor`：行级 diff 区间不相交则合并
  - 冲突 → 409 + `{conflict: {local, remote, base}}` 片段
  - _需求：5.4_

- [ ] 14. 批注 sidecar
  - `data/files/<room>/<file_id>.comments.json`；`GET/POST/PATCH/DELETE .../files/{id}/comments[/{cid}]`
  - 权限 = 文件编辑权；随主文件删除而删除；**不占文件列表 200 配额**
  - 2D 预览按 anchor 定位渲染气泡层；XR 面板在对应位置打标记点 + 展开文本
  - _需求：5.6_

- [ ] 15. 模型族补充 `obj` / `stl`
  - 插件 `obj.js`（OBJLoader + MTLLoader，贴图缺失降级为无色）+ `stl.js`（STLLoader，ASCII/二进制都支持）
  - 识别 `.obj`/`.mtl`/`.stl`；`toEntity` + `measure`；摆入 cost=1
  - _需求：4.2_

- [ ] 16. **`geo3d` 三维地图（XR 旗舰之二）**
  - 插件 `geo3d.js`：GeoJSON Polygon/MultiPolygon → `THREE.ExtrudeGeometry`（高度取 `properties.height` 或统一值）；Point `properties.height` → 柱体；LineString → `TubeGeometry`
  - 自动居中 + 按范围缩放；`toEntity` + `measure`；cost=2
  - `template`：一小片街区骨架；服务端 `validate` 顶层 schema
  - `kml`（M3）预留同一插件内分支
  - _需求：4.2、4.3_

- [ ] 17. `deck` Markdown 演示稿 + XR 演示厅
  - 插件 `deck.js`：按 `---` 分页；2D 为幻灯片播放；`toTexture` 为**当前页**纹理
  - 摆入 `placement=panel`；XR 内翻页控制（摇杆左右 / 双手手势）
  - 识别 `.deck.md` 或 `?as=deck`
  - _需求：4.5_

- [ ] 18. 树形/文本查看器族 `json` / `yaml` / `xml` / `code` / `log` / `sql_schema` / `srt`
  - `json.js`（可折叠树，大文件惰性展开）、`code.js`（引入 highlight.js，按扩展名选语言，>200KB 关闭高亮）、`srt.js`（时间轴列表 + 若能关联同房 video 则叠加显示）、`yaml/xml`（复用 json 树视图 + 解析降级）
  - _需求：3.2、3.5_

- [ ] 19. 2D 图族 `graphviz` / `excalidraw` / `lottie`
  - `graphviz.js`（viz.js wasm，Worker 内，预算 5s）、`excalidraw.js`（自写元素渲染：rectangle/ellipse/arrow/text/freedraw，只读先行 → 可写留 M3）、`lottie.js`（lottie-web，**关闭表达式求值**）
  - lottie `security=sanitized`；graphviz `security=isolated`
  - _需求：3.2、3.4、7.2_

- [ ] 20. 媒体播放器对象（`audio` 的 XR 呈现，设计见 design.md 十二节）
  - [ ] 20a. 资产生成：Blender Python 脚本 → `static/models/media-player.glb`（老式录音机；单文件自包含、+Z 正面、原点底面中心、约 0.34×0.18×0.24m、≤15k 面、1 张 ≤1024² baseColor + 屏幕 emissive）。**节点命名契约必须齐**：`Body`/`Button_Play`/`Button_Stop`/`Button_Next`/`Screen`/`Speaker`/`Reel`/`Led_Play`，材质 `Mat_Body`/`Mat_Screen`/`Mat_Button`。脚本进版本控制、参数化（配色/按键数/屏幕比例）
  - [ ] 20b. `placement.mount` 扩展 `entity|panel|player|none`；`audio` 注册表条目加 `xr:"player"`、`mount:"player"`、`model:"media-player"`、`behavior:"audio-player"`、`cost:1`；服务端仍只存位姿
  - [ ] 20c. `xr-files.js` 加载器：GLB 模块级缓存（**永不 dispose**）+ `SkeletonUtils.clone()` 多实例；**为 player 类型写独立 dispose 路径**（只删每实例的 CanvasTexture / panner / source，绝不 `disposeObjectTree` 共享几何——2.18 那个 helper 是给独享几何写的）
  - [ ] 20d. `XrMediaPlayer` 类（新建 `static/xr/xr-media-player.js`）：fetch → `decodeAudioData` → AudioBuffer 按 fileId 缓存；`play/pause/seek/stop` 直接控 `AudioBufferSourceNode`（`start(0, offset)`）；panner 挂 `Speaker` 节点（`refDistance 1.6` / `rolloff 1.4`，与既有语音一致）；**不复用 2D 抽屉的 `<audio>` 元素**（`createMediaElementSource` 唯一性约束，见 `xr-main.js:314`）；回退链：buffer → 元素 → 平铺 → 引导 2D
  - [ ] 20e. 交互：`Button_*` 命中优先于 `Body`（父链找 `userData.__role==="control"`）→ 按播放/暂停；命中 `Body` 才进 adjBar；**调整模式中按钮交互关闭**；按键按下沿 -Y 平移 0.004m 回弹（150ms）；播放时 `Reel` 旋转 + `Led_Play` 点亮
  - [ ] 20f. 屏幕与进度条：`Screen` 上 512×128 CanvasTexture（文件名跑马灯 + 进度条 + `1:23 / 4:05` + 状态图标）；**仅播放中重绘、约 10fps**，多台时最近一台 10fps 其余 2fps；同一 canvas 复用给聚焦放大面板（1024×256，带可拖拽 seek、音量、上/下一首）
  - [ ] 20g. 权限与降级：播放 = 读权限（无需 `canEditFiles`）；`filesLocked` 只锁摆位不锁播放；GLB 加载失败 → 程序化盒体（音频功能保留）；AudioContext 手势 `resume()` 复用 `spatialHook` 那处；suspended 时屏幕提示「点击以启用声音」
  - [ ] 20h. e2e：音频摆放后出现实体、按键播放/暂停、调整中不误触、无编辑权可播放不可摆位、锁定下可播放、删 GLB 后仍可播放、两台互不干扰且移除一台不影响另一台外观
  - _需求：4.1–4.3、5.6（读权限语义）_
  - 附：`2D` 端不变（`<audio>` 播放器照旧）；同步「播音模式」（全房间同步播放，±100–300ms 漂移）为 M3，见 design.md 12.8

- [ ] 21. 文档、e2e 与发布
  - `/skill.md`：patch 三种 mode 示例、批注接口、并发最佳实践（先 patch → 再 baseUpdatedAt → 最后整文覆盖）、6 种共创模式说明（见 design.md 五节）
  - `e2e.sh`：patch 三定位、锚点 422 + 锚点列表、三方合并 `merged:true`、真实冲突 409、批注 CRUD、geo3d/chart3d 摆入、deck 翻页状态
  - 版本 2.23.0
  - _需求：5.8、6.1（转换仍不做）、10.6_

---

## 里程碑 M3：建筑 / 音乐 / 影视三大工种（目标 2.24.0）

- [ ] 22. `ifc` BIM 走查 —— 引入 `web-ifc` wasm（Worker + 解析预算），`ifc.js` 插件：`toEntity` 生成构件网格；cost=3（预算制保证同屏不超载）；2D 端只显示构件树摘要
- [ ] 23. `kml` 地图标注（复用 geo3d 插件的几何原语）
- [ ] 24. 音乐族：`midi`（`@tonejs/midi` + Tone.js；2D 钢琴卷帘 + 播放；XR 乐器立体编排 + 空间音频）、`musicxml`（osmd 渲染五线谱 → 面板「空中乐谱架」）、`tone_patch`（JSON → Web Audio 合成 + 可摆放声源）、`audio_scene`（多声源空间布置 JSON）
- [ ] 25. 影视族：`video360`（等距柱状球面映射，环视/球幕）、`otio`（OpenTimelineIO JSON → 时间线轨道渲染 + 与同房 video 联动预览）、`edl`（纯文本剪辑表）、`hyperframes`（iframe 沙箱预览 HTML 合成）
- [ ] 26. 知识族：`latex`（KaTeX；`$…$` / `$$…$$` 与 `\begin{}` 分段渲染）、`notebook`（.ipynb 静态渲染：markdown + 代码高亮 + 输出图，**永不执行**）、`ply` 点云、`fbx`（FBXLoader addon）、`openapi`/`protobuf`/`json_schema`/`diff`/`toml`/`photo_exif`
- [ ] 27. `excalidraw` 升级为可写（XR 白板书写、2D 拖拽绘图），写回走整文 LWW
- [ ] 28. 文档、e2e、发布（版本 2.24.0）

---

## 里程碑 M4：转换管线与探索池（按需立项，逐个小版本）

> **不一次性做**。每项先确认有真实需求，再单独立项；下列顺序按「价值 / 成本」粗排。

- [ ] 29. 转换管线骨架（服务端）：`POST .../files/{id}/convert` `{to: docType}` → 产物为**新共同文件**；原文件不变，列表显示「有文本版」徽标；注册表声明 `convert` 方向；失败明确降级为仅存储
- [ ] 30. Office 文本正门：`fodt`/`fods`/`fodp`（纯 XML 直接渲染，XSLT 或自写子集）、`flat_opc`（Flat OPC XML）
- [ ] 31. Office 二进制转换：`docx→markdown`、`xlsx→csv`、`pptx→deck`、`odt/ods/odp→fodt/fods/fodp`（服务端 Python，zipfile + XML）
- [ ] 32. `cad2d`（ASCII DXF 自写解析，2D 线框 + 可抬高为 3D 板）、`gerber`、`kicad_pcb`/`kicad_sch`（s-expression）
- [ ] 33. 探索池（每项独立评估 wasm 体积与收益）：`scad`（openscad-wasm）、`step`/`iges`（occt-import-js）、`usda`/`usdz`、`3mf`、`splat`(3DGS)、`pcd`、`citygml`、`osm`、`3dtiles`、`gcode`（走刀动画）、`abc`、`strudel`、`lilypond`、`manim`、`remotion`、`rive`、`psd`、`bib`、`org`、`rst`、`drawio`、`vega`、`plantuml`、`fcpxml`、`photo_exif`
- [ ] 34. 软分组（`projectId` + `role`）：仅在 M1–M3 中**确实观察到**多文件产物需求时立项（否则永久推后）
- [ ] 35. 明确不做（写进文档避免反复讨论）：实时协同 OT/CRDT、服务端渲染视频、版本历史/回滚/分支、完整目录树、在线运行代码、第三方云预览

---

## 依赖与风险

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| wasm 解析器体积（IFC/STEP/scad 均 ~10MB） | 首屏与流量 | 一律 Worker + 懒加载 + CDN/本地 vendor 双通道；不做则降级信息卡 |
| 渲染器插件数量膨胀 | 维护成本 | 统一接口 + 单测（每插件一份最小内容 fixture，e2e 断言渲染不抛错） |
| patch 三方合并的正确性 | 数据覆盖 | 合并失败一律 409 交还调用方；**永不**静默丢弃任一侧改动 |
| `doc_type` 与 `kind` 双轨 | 概念混乱 | 文档统一口径：kind = 存储分类（不变），docType = 渲染语义（新增）；两者是多对一，绝不让 docType 影响存储与上限 |
| XR 摆入预算 | 房间塞满导致掉帧 | cost 权重 + 分列上限；超限 400 并提示 |
