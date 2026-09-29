# 设计文档 — 房间共创工作台（Room Doc Workbench）

## 架构总览

在 2.18「共同文件」之上加**一层能力体系**，不加新的传输通道，不改存储语义。

```
        数据层（不变）                     能力层（新增）                       渲染层（插件化）
  data/files/<room>/<id><ext>      app/doc_types.py  ← 唯一事实来源      static/doc-renderers/<docType>.js
  room_files (元数据)         ───▶  doc_type + capabilities  ───▶  2D: {canRender, render(el, content)}
  rooms.files_revision              GET /api/doc-types                 XR: {toPanel(), toEntity(), toTexture()}
  （kind / LWW / 权限 / 归档不动）     patch / convert / comments         Agent: 同一份 capability 表
```

三条不变式：

1. **kind 不动，doc_type 新增**。`kind` 仍是「二进制还是文本、上限多少」的存储分类；`doc_type` 是「怎么渲染、能不能改」的语义分类。多对一：`chart`/`csv`/`mermaid` → kind=`text`。
2. **服务器不认识几何**。注册表里只是字符串与布尔位；解析、排版、三角化全在客户端。
3. **渲染器可缺席**。任何 doc_type 客户端没有实现 → 回落 kind → 回落信息卡。旧客户端零改动。

## 一、文档类型总清单（核心）

> 列含义：**档位** = 该类型在 WebXR 里的**呈现效果评级**（★★★ 原生 3D 实体 / ★★ 空间面板＋空间音频或影院 / ★ 空间面板 / ☆ 仅信息卡）——它回答「**值不值得**搬进头显」。与之正交的是 §十三 的**预览三型 P1/P2/P3**，回答「**用什么机制**渲染」。**Agent** = 文本可编辑性（✅ 纯文本直写 / ◐ 部分文本（含二进制依赖）/ ❌ 二进制需转换）。**难度** = 客户端渲染实现难度。**阶段** = 见第四节路线图。

### 1. 三维模型与实体（WebXR 主场）

| doc_type | 名称 | 扩展名 | 格式性质 | kind | Agent | 2D 预览 | WebXR 预览 | 档位 | 人类工种 / 用途 | 难度 | 阶段 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `gltf` | glTF 场景 | .gltf（+ .bin/贴图） | JSON（可自包含） | model | ◐ | 缩略图 | 实体，可环视 | ★★★ | 3D 美术、产品设计、Web 3D | 低（已有 GLTFLoader） | M2 |
| `glb` | 二进制 glTF | .glb | 二进制容器 | model | ❌ | 缩略图 | 实体 | ★★★ | 同左（交付形态） | 已有 | 已交付 |
| `vrm` | VRM 虚拟形象 | .vrm | 二进制（glTF 扩展） | model | ❌ | 缩略图 | 实体（含表情/骨骼） | ★★★ | 虚拟形象、演出 | 已有 | 已交付 |
| `obj` | OBJ 网格 | .obj（+ .mtl） | **纯文本** | model | ✅ | 缩略图 | 实体 | ★★★ | 3D 建模、扫描件、游戏资产 | 低（OBJLoader+MTLLoader） | M2 |
| `stl` | STL（ASCII） | .stl | **ASCII 文本**（有二进制变体） | model | ✅ | 缩略图 | 实体 | ★★★ | **3D 打印**、机械件、手办 | 低（STLLoader） | M2 |
| `ply` | PLY 点/面云 | .ply | **ASCII 文本**（有二进制变体） | model | ◐ | 缩略图 | 实体（点云/网格） | ★★★ | 扫描重建、科研点云 | 低（PLYLoader） | M3 |
| `usda` | USD 场景（文本） | .usda | **纯文本** | model | ✅ | 缩略图 | 实体（子集） | ★★★ | 影视/工业场景标准、合成数据 | 高（无成熟 JS 加载器，需自写子集） | M4 探索 |
| `usdz` | USD 压缩包 | .usdz | 二进制（zip） | model | ❌ | 缩略图 | 需转换 | ☆ | AR 快速查看、电商 3D | 高 | M4 探索 |
| `3mf` | 3MF 打印件 | .3mf | zip + XML | model | ◐ | 缩略图 | 解包后实体 | ★★ | 3D 打印（含材质/单位） | 中 | M4 探索 |
| `fbx` | FBX | .fbx | 二进制（有 ASCII 变体） | model | ❌ | 缩略图 | 实体 | ★★★ | 动画/影视管线 | 中（FBXLoader addon） | M3 |
| `splat` | 高斯泼溅点云 | .ply/.splat/.ksplat | 二进制 | model | ❌ | 缩略图 | 实体（3DGS） | ★★★ | 实景三维重建、房产看房 | 中 | M4 探索 |
| `pcd` | 点云（ASCII） | .pcd | **ASCII 文本** | model | ✅ | 列表 | 实体（点云） | ★★★ | 机器人/自动驾驶点云 | 中 | M4 探索 |

### 2. CAD / 工程（文本化程度意外地高）

| doc_type | 名称 | 扩展名 | 格式性质 | kind | Agent | 2D 预览 | WebXR 预览 | 档位 | 人类工种 / 用途 | 难度 | 阶段 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `cad2d` | DXF 二维图 | .dxf | **ASCII 文本**（有二进制变体） | text | ✅ | 线框渲染 | 面板（可抬高为 3D 板） | ★★ | **机械/建筑制图**、激光切割 | 中（自写 ASCII DXF 解析） | M3 |
| `scad` | OpenSCAD 参数化模型 | .scad | **纯文本代码** | text | ✅ **（黄金：代码→3D）** | 源码 + 静态渲染 | 实体（改参数即改形） | ★★★ | **参数化工业设计**、Maker | 高（openscad-wasm ~10MB，Worker） | M4 探索 |
| `step` | STEP 实体模型 | .step/.stp | **纯文本**（ISO 10303） | text | ◐（结构复杂） | 源码 | 实体（三角化后） | ★★★ | **工业 CAD 交换标准**、装配 | 高（occt-import-js wasm） | M4 探索 |
| `iges` | IGES | .igs/.iges | 文本（列式） | text | ◐ | 源码 | 实体 | ★★ | 老工业/CAM 管线 | 高 | M4 探索 |
| `gcode` | G-code 刀路 | .gcode/.nc | **纯文本** | text | ✅ | 源码 + 2D 轨迹 | 实体（**走刀动画**） | ★★★ | 数控加工、3D 打印路径 | 中（自写解析，很直观） | M4 探索 |
| `kicad_pcb` | KiCad PCB | .kicad_pcb | **s-expression 文本** | text | ✅ | 板框 + 布线 | 实体（3D 板卡） | ★★★ | **电子硬件设计** | 高 | M4 探索 |
| `kicad_sch` | KiCad 原理图 | .kicad_sch | **s-expression 文本** | text | ✅ | 原理图 | 面板 | ★ | 电子电路设计 | 高 | M4 探索 |
| `gerber` | Gerber 制造图 | .gbr/.gerber | 文本（RS-274X） | text | ✅ | 图层渲染 | 面板 | ★ | PCB 制造（工厂交付） | 中 | M4 探索 |
| `svg` | SVG 矢量图 | .svg | **文本 + 图像双性** | svg | ✅ | 内联图 | 面板纹理 | ★ | 平面设计、Logo、激光切割、图标 | 已有 | 已交付 |

### 3. 三维地图 / 建筑 / 地理（WebXR 主场之二）

| doc_type | 名称 | 扩展名 | 格式性质 | kind | Agent | 2D 预览 | WebXR 预览 | 档位 | 人类工种 / 用途 | 难度 | 阶段 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `geo3d` | GeoJSON 三维地图 | .geojson | **JSON** | text | ✅ | 平面地图 | 实体（拉伸建筑/地块） | ★★★ | 城市规划、GIS、地图应用 | 中（自写 extrude） | M2 |
| `kml` | KML 地图 | .kml | **XML** | text | ✅ | 平面地图 | 实体（路线/多边形/地标） | ★★★ | 地理标注、巡线、旅行规划 | 中 | M3 |
| `citygml` | CityGML 城市模型 | .gml/.citygml | **XML** | text | ◐ | 列表 | 实体（LOD1/2 建筑群） | ★★★ | 数字孪生城市、BIM+GIS | 高 | M4 探索 |
| `ifc` | IFC 建筑信息模型 | .ifc | **纯文本**（STEP-like） | text | ◐ | 构件列表 | 实体（**BIM 走查**） | ★★★ | **建筑设计、施工、运维** | 高（web-ifc wasm，生态成熟） | M3 |
| `osm` | OpenStreetMap 数据 | .osm/.xml | **XML** | text | ✅ | 平面地图 | 实体（路网/建筑） | ★★★ | 开放地图、路网分析 | 中 | M4 探索 |
| `3dtiles` | 3D Tiles | tileset.json + .b3dm | JSON + 二进制 | text | ◐ | 列表 | 实体（LOD 流式城市） | ★★★ | 实景三维、城市级大场景 | 高 | M4 探索 |
| `csv_geo` | 经纬度点表 | .csv | **纯文本** | text | ✅ | 表格 + 平面散点 | 实体（地标柱） | ★★ | 数据分析、门店分布 | 低 | M2 |

> `geo3d` 是「WebXR 优势 × 文本可编辑」的最佳交汇点之一：Agent 能直接生成/修改经纬度 JSON，人类在房间里绕着城市转。建议作为 M2 的旗舰示例。

### 4. 二维图表 / 图 / 白板

| doc_type | 名称 | 扩展名 | 格式性质 | kind | Agent | 2D 预览 | WebXR 预览 | 档位 | 人类工种 / 用途 | 难度 | 阶段 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `chart` | 数据图表 | .chart.json | **JSON**（ECharts option） | text | ✅ | ECharts 渲染 | 面板纹理 | ★ | 数据分析、报表、经营看板 | **极低（echarts 已在 vendor）** | **M1** |
| `chart3d` | 三维图表 | .chart3d.json | **JSON**（自定义 schema） | text | ✅ | three 离屏缩略图 | **实体（3D 柱/散点/曲面）** | ★★★ | 数据可视化、汇报、教学 | 中（自写 mapper） | **M1** |
| `csv` | 表格数据 | .csv/.tsv | **纯文本** | text | ✅ | 表格视图 | 面板（可转 chart/chart3d） | ★ | 几乎一切白领工作 | 低 | **M1** |
| `mermaid` | Mermaid 图 | .mermaid/.mmd | **纯文本** | text | ✅ | Mermaid 渲染 | 面板纹理 | ★★ | 流程图、时序图、甘特、类图、脑图 | 已有管线（独立成类型） | **M1** |
| `markmap` | 思维导图 | .markmap.md | **Markdown** | markdown | ✅ **（Agent 天然会写）** | markmap 渲染 | 面板/3D 节点树 | ★★ | 知识梳理、脑暴、大纲 | 低（markmap 库） | M2 |
| `graphviz` | Graphviz DOT 图 | .dot/.gv | **纯文本** | text | ✅ | viz.js 渲染 | 面板纹理 | ★ | 依赖图、网络拓扑、组织图 | 中（viz.js wasm） | M2 |
| `plantuml` | PlantUML 图 | .puml/.plantuml | **纯文本** | text | ✅ | 服务端/wasm 渲染 | 面板纹理 | ★ | 软件设计（UML 全家桶） | 中高（wasm 重） | M3 |
| `excalidraw` | 手绘白板 | .excalidraw | **JSON** | text | ✅ | 自写 Canvas 渲染 | **面板（可书写的白板）** | ★★ | 白板协作、草图、教学 | 中（schema 简单） | M2 |
| `drawio` | draw.io 图 | .drawio/.xml | XML（可压缩） | text | ◐ | 只读渲染 | 面板纹理 | ★ | 架构图、网络图（企业常用） | 中高 | M4 探索 |
| `vega` | Vega-Lite 图表 | .vl.json | **JSON** | text | ✅ | vega 渲染 | 面板纹理 | ★ | 学术/数据新闻、可复现图表 | 中（vega-lite 库） | M3 |
| `a2ui` | 声明式数据面板 | （聊天块/文件） | **JSON** | text | ✅ | 组件渲染（已有） | 面板纹理 | ★ | 卡片式信息面板、看板 | 已有协议 | M2 |

### 5. 动画与运动

| doc_type | 名称 | 扩展名 | 格式性质 | kind | Agent | 2D 预览 | WebXR 预览 | 档位 | 人类工种 / 用途 | 难度 | 阶段 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `lottie` | Lottie 动画 | .lottie.json | **JSON** | text | ◐（结构复杂但有工具） | lottie-web 播放 | 面板（**动画贴片**） | ★★ | 动效设计、UI 动画、吉祥物 | 低（lottie-web） | M2 |
| `svg_anim` | SVG 动画 | .svg | **文本（SMIL/CSS）** | svg | ✅ | 内联动画 | 面板（动态纹理） | ★ | 图标动画、轻量动效 | 低 | M2 |
| `manim` | Manim 动画脚本 | .py | **纯文本代码** | text | ✅ | 源码 | 需服务端渲染 | ☆ | 数学/科普动画 | 高（需服务端渲染链路） | M4 探索 |
| `remotion` | Remotion 视频脚本 | .tsx | **纯文本代码** | text | ✅ | 源码 | 需服务端渲染 | ☆ | 程序化视频、批量生成 | 高 | M4 探索 |
| `rive` | Rive 交互动画 | .riv | 二进制 | other | ❌ | 运行时渲染 | 面板（可交互） | ★ | 交互式 UI 动效、状态机 | 中（runtime wasm） | M4 探索 |

### 6. 音乐 / 声音（WebXR 空间音频优势）

| doc_type | 名称 | 扩展名 | 格式性质 | kind | Agent | 2D 预览 | WebXR 预览 | 档位 | 人类工种 / 用途 | 难度 | 阶段 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `midi` | MIDI 编曲 | .mid/.midi | 二进制（结构化） | other | ◐ | 钢琴卷帘 + 播放 | **实体（乐器立体编排 + 空间音频）** | ★★★ | **音乐制作、编曲、教学** | 中（@tonejs/midi + Tone.js） | M3 |
| `musicxml` | MusicXML 乐谱 | .musicxml/.mxl | **XML**（.mxl 是 zip） | text | ✅ | 五线谱渲染 | 面板（**空中乐谱架**） | ★★ | 作曲、排练、出版 | 中高（osmd 库） | M3 |
| `abc` | ABC 记谱法 | .abc | **纯文本** | text | ✅ **（Agent 易生成）** | 五线谱 | 面板 | ★ | 民谣/教学记谱、快速原型 | 中（自写简易渲染） | M4 探索 |
| `lilypond` | LilyPond 乐谱 | .ly | **纯文本代码** | text | ✅ | 需编译 | 需转换 | ☆ | 专业排版乐谱 | 高 | M4 探索 |
| `tone_patch` | 合成器音色 | .tone.json | **JSON** | text | ✅ | 波形 + 试听 | **空间音频源（可摆放声源）** | ★★★ | 音色设计、声音装置艺术 | 中（Web Audio 原生） | M3 |
| `strudel` | Live-coding 音乐 | .strudel/.tidal | **纯文本** | text | ✅ | 播放 + 代码高亮 | 空间音频 | ★★ | 电子音乐、算法作曲演出 | 中 | M4 探索 |
| `audio` | 音频文件 | .mp3/.wav/.ogg/.flac/.m4a | 二进制 | audio | ❌ | 播放器 | **空间音频源** | ★★ | 配音、播客、音效 | 已有（XR 侧待补） | M2 |
| `audio_scene` | 空间音频场景 | .soundscene.json | **JSON** | text | ✅ | 列表 | 实体（多声源布置） | ★★★ | 声音装置、沉浸式展览 | 中 | M3 |

### 7. 视频 / 时间线（XR 影院）

| doc_type | 名称 | 扩展名 | 格式性质 | kind | Agent | 2D 预览 | WebXR 预览 | 档位 | 人类工种 / 用途 | 难度 | 阶段 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `video` | 视频 | .mp4/.webm/.mov/.m4v | 二进制 | video | ❌ | 播放器 | **影院屏（VideoTexture）** | ★★★ | 影视、培训、演示 | 已有 | 已交付 |
| `video360` | 全景/VR 视频 | .mp4（等距柱状） | 二进制 | video | ❌ | 拖拽全景 | **球幕（环视沉浸）** | ★★★ | VR 内容、旅游、房产 | 中（球面映射） | M3 |
| `srt` | 字幕 | .srt/.vtt/.ass | **纯文本** | text | ✅ | 字幕列表/时间轴 | **空间浮动字幕** | ★★ | 视频本地化、无障碍 | 低 | M2 |
| `otio` | 剪辑时间线 | .otio | **JSON**（OpenTimelineIO） | text | ✅ | 时间线轨道 + 帧预览 | 面板（时间线 + 影院屏联动） | ★★ | **视频剪辑、影视后期** | 中（自写时间线渲染） | M3 |
| `edl` | 剪辑表 | .edl | **纯文本** | text | ✅ | 时间线表 | 面板 | ★ | 传统剪辑交换 | 低 | M3 |
| `fcpxml` | FCPXML | .fcpxml | **XML** | text | ◐ | 时间线 | 面板 | ★ | Final Cut 工作流 | 中 | M4 探索 |
| `hyperframes` | HyperFrames 合成 | .html + manifest | **HTML/JS 文本** | text | ✅ | iframe 沙箱预览 | 面板（成片播放） | ★★ | 动效/视频生成（本项目已有技能链） | 中 | M3 |

### 8. 办公文档与演示

| doc_type | 名称 | 扩展名 | 格式性质 | kind | Agent | 2D 预览 | WebXR 预览 | 档位 | 人类工种 / 用途 | 难度 | 阶段 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `markdown` | Markdown 文档 | .md/.markdown | **纯文本** | markdown | ✅ **（主战场）** | 富文本渲染 | 面板（可书写） | ★ | 纪要、方案、文档、README | 已有 | 已交付 |
| `deck` | Markdown 演示稿 | .deck.md | **Markdown**（Marp/Slidev 约定） | markdown | ✅ **（Agent 能写 PPT）** | 幻灯片播放 | **面板（XR 演示厅，可翻页）** | ★★ | 汇报、路演、教学 | 中（复用 markdown + 分页） | M2 |
| `latex` | LaTeX 文档 | .tex | **纯文本代码** | text | ✅ | KaTeX/MathJax 渲染 | 面板（公式板） | ★ | 学术论文、公式、讲义 | 中（KaTeX） | M3 |
| `fodt` | ODF 文本文档 | .fodt | **纯 XML**（单文件） | text | ◐ | 转 HTML 渲染 | 面板 | ★ | 文字处理（LibreOffice 交换） | 中（XSLT/自写子集） | M4 探索 |
| `fods` | ODF 电子表格 | .fods | **纯 XML**（单文件） | text | ◐ | 表格渲染 | 面板 | ★ | 表格/财务（文本化可 Agent 改） | 中 | M4 探索 |
| `fodp` | ODF 演示稿 | .fodp | **纯 XML**（单文件） | text | ◐ | 幻灯片渲染 | 面板 | ★★ | 演示（文本化 PPT） | 中 | M4 探索 |
| `docx`/`xlsx`/`pptx` | MS Office | .docx/.xlsx/.pptx | 二进制（zip+XML） | other | ❌ | **需转换** | 转换后渲染 | ☆ | 通用办公（最大存量） | 中（转换管线） | M4 探索 |
| `flat_opc` | 扁平 OOXML | .xml（Flat OPC） | **纯 XML**（单文件） | text | ◐ | 转 HTML | 面板 | ★ | Office 的文本化逃生舱 | 中 | M4 探索 |
| `odf_pkg` | ODF 包 | .odt/.ods/.odp | 二进制（zip） | other | ❌ | 需转换 | 转换后渲染 | ☆ | LibreOffice 交付 | 中 | M4 探索 |

> **关键洞察**：Office 系并非只能靠转换——`fodt`/`fods`/`fodp` 与 `flat_opc` 都是**单文件纯 XML**，天然满足「Agent 可改」，且 LibreOffice 可无损互转。这是把 Office 纳入文本共创体系的正门，比服务端转 docx→html 的派生物更干净。

### 9. 数据、代码与知识

| doc_type | 名称 | 扩展名 | 格式性质 | kind | Agent | 2D 预览 | WebXR 预览 | 档位 | 人类工种 / 用途 | 难度 | 阶段 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `json` | JSON 数据 | .json | **纯文本** | text | ✅ | 树形查看器 + 可折叠 | 面板（可转 chart3d） | ★ | API、配置、数据交换 | 低 | M2 |
| `yaml` | YAML | .yaml/.yml | **纯文本** | text | ✅ | 树形查看器 | 面板 | ★ | 配置、CI、K8s | 低 | M2 |
| `xml` | XML | .xml | **纯文本** | text | ✅ | 树形查看器 | 面板 | ★ | 数据交换、配置 | 低 | M2 |
| `toml` | TOML | .toml | **纯文本** | text | ✅ | 树形查看器 | 面板 | ★ | 配置 | 低 | M3 |
| `code` | 源代码 | .py/.js/.ts/.go/.rs/.c/… | **纯文本** | text | ✅ | 语法高亮 + 行号 | 面板（**共享代码屏**） | ★ | 软件工程全谱 | 低（highlight.js） | M2 |
| `notebook` | Jupyter Notebook | .ipynb | **JSON** | text | ✅ | 静态渲染（**不执行**） | 面板 | ★ | 数据科学、实验记录 | 中 | M3 |
| `sql_schema` | 数据模型 / DDL | .sql | **纯文本** | text | ✅ | 高亮 + ER 图（可选） | 面板 | ★ | 数据库设计 | 低 | M2 |
| `openapi` | API 规范 | .yaml/.json | **纯文本** | text | ✅ | 接口文档 + 试调（可选） | 面板 | ★ | 后端/接口协作 | 中 | M3 |
| `protobuf` | Protobuf 定义 | .proto | **纯文本** | text | ✅ | 高亮 | 面板 | ★ | 服务间协议 | 低 | M3 |
| `json_schema` | JSON Schema | .schema.json | **纯文本** | text | ✅ | 表单预览（可选） | 面板 | ★ | 数据校验、表单生成 | 中 | M3 |
| `log` | 日志 | .log | **纯文本** | text | ✅ | 尾随视图 | 面板 | ★ | 运维、调试 | 低 | M2 |
| `diff` | 补丁 | .diff/.patch | **纯文本** | text | ✅ | 并排差异渲染 | 面板 | ★ | 代码审阅 | 低 | M3 |
| `bib` | 参考文献 | .bib | **纯文本** | text | ✅ | 引用列表 | 面板 | ★ | 学术写作 | 低 | M4 探索 |

### 10. 图像

| doc_type | 名称 | 扩展名 | 格式性质 | kind | Agent | 2D 预览 | WebXR 预览 | 档位 | 人类工种 / 用途 | 难度 | 阶段 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `image` | 位图 | .png/.jpg/.jpeg/.gif/.webp/.bmp/.avif | 二进制 | image | ❌ | 内联图片 | **面板（可走近看细节）** | ★★ | 摄影、设计、截图 | 已有 | 已交付 |
| `gif_anim` | 动图 | .gif/.webp | 二进制 | image | ❌ | 动图播放 | 面板（动态纹理） | ★ | 表情、演示 | 已有 | 已交付 |
| `psd` | Photoshop | .psd | 二进制 | other | ❌ | 需转换 | 转换后面板 | ☆ | 平面设计源文件 | 中（转换管线） | M4 探索 |
| `photo_exif` | 带 EXIF 的照片 | .jpg/.heic | 二进制 + 元数据 | image | ◐（EXIF 可文本化） | 图片 + 元数据表 | 面板 | ★ | 摄影归档、地理定位 | 低 | M3 |

### 统计

- **总计 60+ 个 doc_type**，其中 **纯文本或部分文本（Agent 可参与）约 40 个**，占 2/3；**WebXR 原生 3D 实体档（★★★）约 22 个**。
- M1（本期）落地 5 个新类型；M2 约 12 个；M3 约 14 个；M4 为探索池。

## 二、WebXR 3D 渲染优势分级（为什么值得搬进头显）

| 档位 | 含义 | 代表类型 | 相比 2D 网页的**不可替代价值** |
| --- | --- | --- | --- |
| ★★★ 原生 3D 实体 | 在场景世界坐标系中生成 three.js 对象 | glb/gltf/vrm/obj/stl/ply、chart3d、geo3d、kml、ifc、cad2d(抬高)、gcode、scad、midi(乐器编排)、usda、splat | **空间理解**：真实尺度、相对位置、可走查。建筑走查、城市俯瞰、零件装配、刀路回放、立体数据图——屏幕上做不到。 |
| ★★ 空间面板 + 空间音频/影院 | 2D 结果 → 纹理，贴到空间；或声源定位 | mermaid、markmap、excalidraw、deck、lottie、video360、musicxml、audio、otio、srt | **共处一室**：多人同看同一块板、围坐看片、声音来自正确的方向；讨论时能指向具体位置。 |
| ★ 空间面板 | 2D 结果 → 纹理，平铺在空间 | markdown、code、csv、chart、json、ipynb、image | **多屏并行**：房间里同时挂 6 块不同的板，各自可缩放；比浏览器标签页更接近「工作台」。 |
| ☆ 仅信息卡 | 只显示元数据 + 引导 2D | docx、psd、odl_pkg、manim、remotion | 无 3D 增益；先存储、后转换。 |

**判断准则**（写给未来的类型扩展者）：一个类型是否值得做 `native3d`，看它的数据是否**天然带三维坐标或层级**。带坐标（geo3d、gcode、ifc）→ 直接实体；带层级（markmap、json）→ 可用 3D 节点树；纯平面（markdown、code）→ 面板即可，别硬做 3D。

## 三、类型注册表设计

### 服务端 `app/doc_types.py`

```python
DOC_TYPES = {
    "chart3d": {
        "name": {"zh": "三维图表", "en": "3D Chart"},
        "exts": {".chart3d.json"},
        "kind": "text",              # 存储分类（沿用既有 8 类）
        "textEditable": True,        # JSON 直写路径可用
        "agentFriendly": True,       # 推荐 Agent 生成
        "accepts": {"text"},         # ?as= 覆盖时允许的 kind
        "xr": "native3d",            # native3d | panel | card | none
        "web": "thumb",              # 2D 预览档
        "placement": "entity",       # entity | panel | none
        "cost": 2,                   # 摆入预算权重（GLB=1）
        "security": "inert",
        "deps": ["three"],           # 渲染依赖（懒加载依据）
        "validate": "chart3d.schema.json",   # 可选：顶层 schema 校验
        "template": '{"type":"bars","series":[...]}',
        "docs": "…给 Agent 的一段说明…",
    },
    # …
}
```

注册表由**四张表**派生（同一份数据，不同视图）：

| 视图 | 消费者 | 用途 |
| --- | --- | --- |
| `GET /api/doc-types` | 2D / XR / Agent | 完整能力表（含 template 与 docs） |
| `GET /api/rooms/{r}/files` 每条的 `docType`+`capabilities` | 2D / XR | 单文件渲染决策（**快照**，客户端可忽略） |
| `/skill.md` 的类型章节 | Agent | 生成，含示例与 patch 建议 |
| `app/doc_types.py::classify()` | 服务器 | 与 kind 同一次嗅探，产出 doc_type |

### 客户端渲染器插件

```
static/doc-renderers/
  _registry.js          # 加载器：docType → dynamic import()
  chart.js              # { canRender(docType), render(el, content, ctx) }
  chart3d.js            # { canRender, toEntity(THREE, content), thumb() }
  csv.js
  mermaid.js
  geo3d.js
  …
```

插件导出**统一接口**，两端共用同一份内容解析：

```js
export default {
  canRender: (docType) => docType === "geo3d",
  // 2D：把内容渲染进一个 DOM 元素
  render(el, content, ctx) { … },
  // XR：产出纹理（面板式）或 three 对象（实体式）
  async toTexture(content) { … },      // → CanvasTexture
  async toEntity(THREE, content, ctx) { … },  // → Object3D（含 dispose()）
  // 可选：本地摆入前的包围盒归一化钩子（复用 2.18 约定）
  measure(object3d) { … },
};
```

主程序按 `capabilities.xr` 决定调 `toEntity` 还是 `toTexture`；**没有插件的 doc_type 直接走 kind 回落**。

## 四、摆入房间的泛化

2.18：只有 `model` 能摆入，形态固定为实体。本规格：

| 承载形态 | 说明 | 位姿语义 | 适用 doc_type |
| --- | --- | --- | --- |
| `entity` | three.js 对象直接进场景 | 沿用 2.18（世界坐标、显式 scale、底边落地、yaw-only） | glb/gltf/vrm/obj/stl/ply、chart3d、geo3d、kml、ifc、gcode、scad、midi |
| `panel` | 2D 渲染结果 → 大画布纹理 → 可缩放面板 | 同一位姿契约；面板有**默认竖立朝向**（面向摆放者）与固定宽高比 | markdown、deck、mermaid、markmap、excalidraw、csv、chart、image、code、musicxml、otio、video360(球幕例外) |
| `placeholder` | **P3 缺省几何体**：薄板 + 文件名纹理 + 类型稳定色相（见 §13.6） | 同一位姿契约；**不可摆入的例外已取消** | docx、psd、odt 等暂无渲染器者（**注意：随渲染器上线会自动升级为 panel/entity，不是永久标签**） |
| `none` | 不提供摆入 | — | 仅剩 `other` 中体积异常或安全受限者（罕见） |

> **修正记录**：本节早先版本写「`card` 档类型 SHALL NOT 提供摆入入口」。按 §十三 的统一效果，**P3 也能摆入**（摆的就是那块写着文件名的牌子），只是计更高 cost。摆入契约对三型完全一致。

**预算制**：把 2.18 的「≤6 个 model」改为

```
sum(cost of placed files) ≤ 12     # cost: GLB=1, chart3d=2, ifc=3, panel=1
entity 数 ≤ 8, panel 数 ≤ 8        # 分列上限，避免一面墙贴满
```

接口形状不变（`PUT .../files/{id}/placement`），仅在服务端增加 cost 求和校验（超限 400）。既有 6 个已摆放的 model 天然满足新预算，**零迁移**。

**面板交互**（XR 内）：adjBar 沿用，新增内容控制——deck 翻页（双手/摇杆）、lottie 播放暂停、otio/gcode 时间轴 scrub、video360 球幕切换（实体式，半径固定）。

## 五、共创编辑协议（patch）

### 三种定位方式

```http
PATCH /api/rooms/{r}/files/{id}/content
{ "mode": "jsonPointer", "baseUpdatedAt": "…",
  "ops": [{"op":"replace","path":"/series/0/data/3","value":42}] }
{ "mode": "anchor", "anchor": "## 风险", "content": "…新的一节…", "position": "after" }
{ "mode": "range", "startLine": 12, "endLine": 14, "content": "…" }
{ "mode": "append", "content": "…" }     # range 的特例，Agent 续写最常用
```

### 冲突处理（三方合并）

```
base（客户端提交的 baseUpdatedAt 对应内容）
   ├── local（客户端想写的内容/ops）
   └── remote（服务器当前内容）
```

- **jsonPointer**：按路径逐 op 判断——远端该路径未变 → 直接应用；变了但可判定「不同路径」→ 仍应用（不同字段互不干扰）；同路径都变 → 该 op 冲突。
- **anchor/range**：按行级 diff，若 local 与 remote 改动**行区间不相交** → 自动合并（`merged:true`）；相交 → 409 + 冲突片段。
- 无 `baseUpdatedAt` → 纯 LWW 直接覆盖（保持既有语义）。

### 批注（sidecar）

```
POST   .../files/{id}/comments        { anchor, text }        → 追加
PATCH  .../files/{id}/comments/{cid}  { resolved?, text? }    → 改
DELETE .../files/{id}/comments/{cid}
GET    .../files/{id}/comments                                 → 列出
```

存储：`data/files/<room>/<file_id>.comments.json`（不占文件列表配额，随主文件删除而删除，随主文件改名不变 id，随替换而保留）。2D 端按 `anchor` 定位渲染气泡；XR 端在面板上打标记点。

### 共创模式（产品视角，写进说明书）

| 模式 | 谁开头 | 典型流程 | 依赖能力 |
| --- | --- | --- | --- |
| A. Agent 生成 → 人类审阅 | Agent | 生成 chart3d/方案.md → 人类批注 → Agent 按批注 patch | template + comments + patch |
| B. 人类起草 → Agent 补全 | 人类 | 人类写骨架 → Agent `append`/`anchor` 补节 | anchor + append |
| C. 双向并发 | 双方 | 人类改文案、Agent 刷数据，各改各的字段/节 | jsonPointer + 三方合并 |
| D. Agent 值班持续维护 | Agent | 轮询 + `sinceRevision`，发现变化即刷新数据源/图表 | patch + 长轮询（对齐既有值班模式） |
| E. 共同布置空间 | 双方 | 人类摆位、Agent 程序化编排（改 placement 的 pose） | placement + API |
| F. 多 Agent 分工 | 多个 Agent | 各自负责不同 projectId 或不同 doc_type | 软分组（需求 9） |

## 六、转换管线（M3+，决策记录）

| 方向 | 执行侧 | 理由 |
| --- | --- | --- |
| `docx/xlsx/pptx → markdown/csv/deck` | 服务端 Python | 无可靠纯前端库；Python 生态成熟（zipfile + XML 解析） |
| `odt/ods/odp → fodt/fods/fodp` | 服务端 Python | 本质是 unzip 单文件化，几十行代码 |
| `fbx → glb` | 服务端（可选） | 需 Blender/assimp，重；列为可选 |
| `psd → png` | 服务端（可选） | 同上 |
| `step/ifc/scad → 三角网格` | **客户端 wasm** | 解析器本来就在浏览器；产物不回写（避免 50MB 网格入库） |

**原则**：转换产物一律是**普通共同文件**（可编辑、可 LWW），原文件保留且标记「有文本版」。转换是**用户或 Agent 显式触发**的动作，不自动执行（避免每次上传都烧 CPU）。

## 七、安全与性能

| 关注点 | 措施 |
| --- | --- |
| 主动内容（svg/html/lottie/a2ui） | `security` 分级；html 强制 iframe 沙箱；lottie 关闭表达式；svg 走 `<img>` 不执行脚本 |
| wasm 解析器（web-ifc/occt/openscad/viz.js） | 一律 Web Worker；解析预算（时间 5s / 内存 / 输入大小）；超限中止 → 信息卡 |
| 代码类文本 | 一律源码高亮，**永不执行**；`.ipynb` 只渲染不运行 |
| 大模型（IFC/STEP/点云） | 不摆入时只做缩略图；摆入时 LOD/抽稀；cost 权重控制同屏数量 |
| 渲染器体积 | 全部 `dynamic import()`，主包不膨胀；vendor 依赖按需拉取 |
| 纹理内存 | 沿用 2.18 纹理 LRU + 退出 dispose + 页面不可见暂停 |
| 服务端 | 不新增解析逻辑（M1–M2），转换（M3+）在受控 worker 中做，输入上限沿用 50MB |

## 八、数据模型变更

```sql
-- 只增，幂等
ALTER TABLE room_files ADD COLUMN doc_type TEXT;      -- 可空；空 = 惰性推导
ALTER TABLE room_files ADD COLUMN project_id TEXT;    -- 可选，M3+
ALTER TABLE room_files ADD COLUMN role TEXT;          -- 可选，M3+
-- 摆入：沿用 world_visible/world_pose，语义泛化到 panel/entity
-- 批注：落盘 data/files/<room>/<file_id>.comments.json，不入库
```

- `doc_type` **不建索引**（列表一次返回 ≤200 条，进程内注册表判断更快）。
- 惰性回填：读取时 `doc_type = row.doc_type or classify_ext(name)`；写入时补齐，不跑迁移脚本。
- `kind` 判定、上限、路径生成**完全不变**。

## 九、路线图

| 里程碑 | 内容 | 规模 | 产出价值 |
| --- | --- | --- | --- |
| **M1** | 类型体系骨架（注册表 + capabilities + 插件接口 + `/api/doc-types`）+ 面板式摆入泛化 + 预算制 + 5 个新类型：`chart`、`chart3d`、`csv`、`mermaid`（独立）、`markmap` | 中 | 立刻让房间「能画数据图」，且 chart3d 体现 XR 优势 |
| **M2** | 2D 图族（graphviz、excalidraw、lottie、vega 待定）+ 模型族（obj、stl）+ `deck` 演示厅 + `geo3d` 3D 地图 + 树形查看器（json/yaml/xml）+ `code` 高亮 + `srt` + `audio` 空间音频 + patch 协议 + 批注 sidecar | 大 | 共创闭环成立（patch + 批注），XR 卖点成型 |
| **M3** | `ifc` BIM 走查、`midi`/`musicxml`/`tone_patch` 音乐、`otio` 时间线 + `video360` 影院、`latex`、`notebook`、`ply`、`fbx`、`kml`、`edl`、`sql_schema` | 大 | 覆盖建筑/音乐/影视三大高价值工种 |
| **M4** | 转换管线（office 系）+ 探索池（`scad`、`step`、`usda`、`splat`、`gcode`、`kicad`、`fodt` 系、`abc`、`strudel`…）按实际需求逐个立项 | 持续 | 长尾覆盖 |

**排序依据**：① 文本可编辑性 × ② XR 增益 × ③ 实现成本。`chart`（库已在）与 `chart3d`（XR 增益最大且成本中）因此排在最前。

## 十、决策记录

| 决策 | 选择 | 理由 |
| --- | --- | --- |
| 扩 kind 还是加 doc_type | **加 doc_type** | kind 是存储/上限分类，改动它要迁移数据与所有上限判断；doc_type 纯新增、可空、惰性推导，零风险 |
| 注册表在服务端还是客户端 | **服务端权威 + 客户端快照** | Agent 需要一份机器可读的能力表；两端不能各维护一份 |
| 渲染器用插件还是大矩阵 | **插件 + 懒加载** | 60 个类型的渲染依赖无法全部进主包；插件接口强制「数据与渲染分离」 |
| 摆入是否限于 model | **泛化为 panel/entity + 预算制** | 面板式摆入是 XR 的核心增量（多屏工作台）；预算制防止房间被塞满 |
| 是否引入版本历史 | **不引入** | 保持 2.18「只存最新」的产品语义；冲突用 patch + 三方合并解决，而非版本树 |
| Office 怎么进 | **优先 fodt/flat_opc 文本格式，docx 走转换** | 文本格式才是 Agent 可改的；转换产物是派生物不是主路径 |
| 3D 内容是否仍一律共同文件 | **是，且扩展到所有 3D 类** | 延续 2.18 内容路由；3D 资产体大、需迭代，不进聊天流 |
| **P2 的预览图从哪来** | **① 原图直用 + ② 客户端复用 2D 渲染器栅格化**（2026-09-28 拍板）；**服务端预生成不做** | 保持服务器「只做透传与最小校验」；不引入无头浏览器，不为单一格式族破例 |
| **P3 是否进房间** | **进，但只作为占位牌子**——有位置、无预览（2026-09-28 拍板） | 房间里永远没有「摆不了的文件」；P1/P2/P3 共用一套摆入契约与 adjBar |
| **三型是否写进数据** | **不写**，每次加载由注册表现算 | 渲染器上线即自动升级全部历史文件，零迁移 |

## 十一、暂缓项（明确不做，避免范围蔓延）

1. **实时协同（OT/CRDT）**：仍无长连接，patch + 三方合并已覆盖常见并发。
2. **服务端渲染（视频/Manim/Remotion）**：需要渲染集群，非本架构范围；M4 仅存源码。
3. **版本历史 / 回滚 / 分支**：与 LWW 语义冲突，明确不做。
4. **目录树**：软分组（projectId）足够；完整目录会引入权限与路径复杂度。
5. **在线运行代码/IPython**：安全红线，只渲染不执行。
6. **第三方云渲染/预览服务**：与「离线可用优先」和隐私原则冲突。
7. **服务端预生成缩略图**：2026-09-28 拍板**不做**（见 §13.4）。它会把服务器从「透传」变成「渲染农场」，并引入无头浏览器依赖。P2 的图只由客户端产出；前端做不出图的格式就留在 P3（有位置、无预览）。

## 十二、媒体播放器对象（Media Player Object）

> 专用于 **`audio` doc_type** 的 XR 呈现：不走「面板」，而是摆一台**老式录音机形状的实体道具**，点按钮即播放、屏幕显示进度。

### 12.1 为什么是道具而不是面板

| | 面板式（默认 panel） | 道具式（本设计） |
| --- | --- | --- |
| 心智 | 一块 UI 贴在空间里 | 房间里**有一台设备** |
| 发现性 | 要找、要打开列表 | 一眼看见、走过去就能按 |
| 共处感 | 各看各的板 | 多人围着同一台机器听 |
| 隐喻 | 文件预览 | **播放设备**（可换磁带、可调音量） |

面板适合「看内容」，道具适合「操作设备」。音频的天然交互是**按一下**，所以选道具。代价是屏幕小、进度条难点，用 12.5 的「聚焦放大」补偿。

### 12.2 挂载模型与注册表

`placement.mount` 从 `entity | panel | none` 扩展为 `entity | panel | player | none`。`audio` 条目新增：

```python
"audio": {
    "name": {"zh": "音频", "en": "Audio"},
    "kind": "audio", "textEditable": False,
    "xr": "player",                 # ← 新档位：实体道具 + 行为
    "mount": "player",
    "model": "media-player",        # → static/models/media-player.glb
    "behavior": "audio-player",     # 交互行为 id，客户端按此绑定
    "placement": "player", "cost": 1,
    "security": "inert",
}
```

- 服务器仍**只存位姿**（`world_pose` 不变），不认识录音机；`?model=` 客户端可换外观，服务器不校验。
- 一个已摆放的音频文件 = 一台录音机。多个音频文件 = 多台，各自独立位姿，各计 cost 1。
- 既有 `model` 的 `mount` 返回 `entity`，旧客户端忽略 `mount` 字段，**零影响**。

### 12.3 GLB 资产契约（交给建模/脚本方）

文件 `static/models/media-player.glb`，**单文件自包含**（贴图烘焙进 GLB，无外部 .bin/贴图），一次加载、多实例克隆。

| 项 | 约定 |
| --- | --- |
| 单位 | 1 unit = 1 m |
| 朝向 | **+Z 为正面**（摆放后面向摆放者，与 2.18「朝向面对摆放者」一致） |
| 原点 | **底面中心**（y=0 落地，接 2.18 的「底边落地」约定） |
| 尺寸 | 约 0.34 × 0.18 × 0.24 m（真实录音机尺度；可被玩家缩放） |
| 面数 | ≤ 15k 三角面（房间可能同时有多台） |
| 纹理 | 1 张 baseColor ≤ 1024²；屏幕用 emissive |
| 动画 | **不需要烘焙动画**——按钮按压缩进、磁带旋转由客户端按节点名程序化驱动 |

**节点命名契约（客户端依赖，不可改名）**：

| 节点名 | 角色 | 客户端如何使用 |
| --- | --- | --- |
| `Body` | 机身壳体 | 拾取拖拽的主体；射线命中它 → 进入 adjBar 调整 |
| `Button_Play` | 播放/暂停键 | 命中即切换播放；按下时沿 **-Y 平移 0.004m** 再回弹（150ms） |
| `Button_Stop` | 停止键（可选） | 停止并归零 |
| `Button_Next` | 下一首（可选） | 切到房间内下一个音频文件 |
| `Screen` | 屏幕面片（正对 +Z） | **挂 CanvasTexture**，画文件名 + 进度条 + 时间；UV 须铺满，1:1 映射 |
| `Speaker` | 喇叭网罩位置 | **PositionalAudio 的挂点**（空节点即可，标记声源位置） |
| `Reel` | 磁带盘（可选） | 播放时 `rotation.z += dt * 3` |
| `Led_Play` | 播放指示灯（可选） | 播放时切材质 emissive 亮度 |

材质命名 `Mat_Body` / `Mat_Screen` / `Mat_Button`，便于客户端按名微调（例如播放时让屏幕泛光）。**节点与材质缺失一律容错**：可选节点没有就跳过对应效果，必需节点（`Body`/`Screen`）缺失则整体降级为 12.10 的程序化盒体。

**生成方式建议**：沿用你既有的「脚本产 GLB」惯例（`make_poster_glb.py` 那一路），用 **Blender Python 脚本**确定性地生成——录音机本质是圆角方体 + 圆柱 + 按键，脚本化成本很低，且**可进版本控制、可复现、可改参数**（配色、按键数量、屏幕比例）。比手作 FBX/GLB 更适合这个项目。

### 12.4 交互模型（两级）

```
射线命中 Button_Play ──▶ 播放/暂停（就地反馈，0 跳转）        ← 一级：一眼就会
射线命中 Body/Screen ──▶ 进入 adjBar 调整 / 聚焦放大控制面板   ← 二级：完整控制
```

- **命中优先级**：先判定 `Button_*`（沿父链找 `userData.__role === "control"`），命中则**不进入调整模式**——避免「想按播放结果拖走了机器」。反之命中 `Body` 才进 adjBar。
- **调整模式下按钮禁用**：进 adjBar 后（12.2 既有 adjBar 的 move/rotate/scale/收起/完成），所有 `Button_*` 的交互暂时关闭，防止拖拽误触播放；退出调整恢复。
- **播放是读权限**：按播放**不需要**文件编辑权（`canEditFiles`），任何能进房间的成员都能按。文件编辑权只决定「能不能摆入/移动这台机器」。
- **锁定语义**：`filesLocked` 只锁摆位（不能移动/收起），**不锁播放**——锁的是「别乱动我的布置」，不是「别听」。
- **聚焦放大**：走近/聚焦时，在道具上方浮出一块面板（复用既有聚焦查看交互），内容与屏幕同源但更大（1024×256），提供**可拖拽的进度条**、音量、上一首/下一首。物理屏幕只做「看一眼」的信息显示。

### 12.5 屏幕与进度条

- 屏幕 = `Screen` 节点上的 `CanvasTexture`，**512×128**；内容：文件名（超长跑马灯）+ 进度条（轨道 + 填充 + 已播放端点）+ `1:23 / 4:05` + 状态图标。
- **只在播放中重绘**，约 **10fps**（不是 60fps）；暂停/空闲时 `needsUpdate` 不置位，零成本。
- **多台并存时分级刷新**：最近的一台 10fps，其余 2fps（时间显示秒级，2fps 足够）。
- 同一块 canvas 放大后**复用**给聚焦面板，避免两套绘制逻辑。

### 12.6 音频实现（关键决策）

**不直接复用 2D 抽屉的 `<audio>` 元素**。原因（已在 `xr-main.js:314` 记录的真实约束）：`createMediaElementSource()` **对同一元素只能调用一次且路由永不可撤销**——2D 播放链已经创建过一次，XR 再建会抛错；退出 XR 还得把声像图接回 destination 兜底。用它做「可 seek 的媒体播放器」会踩这个坑。

**方案：自建 `XrMediaPlayer`，直接控 `AudioBufferSourceNode`。**

```
fetch(contentUrl) → ArrayBuffer → AudioContext.decodeAudioData → AudioBuffer（缓存，按 fileId）
play():
  src = ac.createBufferSource(); src.buffer = buffer;
  src.connect(panner)                      # positionPlayer 处，HRTF
  src.start(0, offset)                     # ← 精确起点，offset 即进度
pause/seek(t): src.stop(); offset = t; play()   # seek = 重建 source
```

优点：① 不碰 MediaElementSource 的唯一性约束；② `start(when, offset)` 天然是**精确 seek** 与**房间同步播放**的正确原语；③ `AudioBuffer` 按 fileId 缓存，重复播放不重新下载；④ 与既有 `spatialVoices` 互不干扰（各自独立的 panner 与 source）。

**回退链**：解码失败或文件 >30MB（`decodeAudioData` 会占大量内存）→ 回退到 `PositionalAudio + MediaElementSource`（此时**必须**是 XR 会话内首次使用该元素，用一个 XR 专属的 `<audio>`，不与 2D 抽屉共用）→ 再回退到平铺 `THREE.Audio`（无 HRTF）→ 最后回退「引导到 2D 网页播放」。

**AudioContext 手势约束**：XR 内首次 `controller select` 事件里 `resume()`（既有 `spatialHook` 已这么做，复用同一处）。若 context 仍 suspended，道具屏幕上显示「点击以启用声音」。

**位置**：`panner` 挂在 `Speaker` 节点的世界坐标；`setRefDistance(1.6)`、`setRolloffFactor(1.4)`（与既有语音一致，保证同房间听感统一），`maxDistance` 15m 后静默。道具旋转/移动时 panner 随节点自动更新（把 panner 挂在 `Speaker` 下即可）。

### 12.7 状态机

```
idle ──按播放──▶ loading ──decode 完成──▶ playing ⇄ paused
                    │                        │
                    └──失败──▶ error ────────┘（显示错误图标 + 引导 2D）
播放结束 ──▶ 回到 idle（进度归零）；单曲不自动循环
文件被替换 ──▶ 保留进度（clamp 到新时长）；文件被删除 ──▶ 整体移除道具
```

### 12.8 同步播放（本地 vs 播音）

| 模式 | 行为 | 实现 | 结论 |
| --- | --- | --- | --- |
| **本地（M2 默认）** | 每人各控各的，互相听不到 | 纯客户端，零服务器状态 | **先做这个** |
| **播音（房间级）** | 一人按下，全房间同步听到同一位置 | 服务端存 `{playing, positionSec, startedAt(服务器时钟), rate}`，随 revision 同步；各端 `start(when, offset)`，`when` 用「服务器时钟 → 本地 AudioContext 时钟」换算 | M3+，需接受漂移 |

**诚实的限制**：本项目无长连接，同步靠轮询 —— 音频同步的**可达精度约 ±100–300ms**（网络延迟 + 时钟偏移），且**只在「开始/暂停/seek」这些离散点同步**，播放过程中靠各端本地时钟自由走（不持续纠偏，否则会听到抽动）。这足够「一起听一首歌」，不够「多声道精确对齐」。说明书必须写明这一点，别让 Agent 以为是 DAW 级同步。

### 12.9 生命周期与性能

- **GLB 只加载一次**（模块级缓存，**永不 dispose**）；每个实例用 `SkeletonUtils.clone()`（vendor 已有）克隆。
- **几何/材质是共享引用**，移除实例时**只 dispose** 每实例独有的 `CanvasTexture`、`AudioBuffer` 引用与 panner/source 节点；**绝不能** `disposeObjectTree(holder)` 一把梭——那是 2.18 对「独享几何」写的，对克隆体用会把共享几何删掉，导致同房间其他录音机变白。**这是本次最需要注意的坑**，需要在 `xr-files.js` 里为 player 类型走独立的 dispose 路径。
- 退出 XR / 房间切换：停播、摘 panner、清 AudioBuffer 缓存引用；沿用既有「退出 dispose」与「页面不可见暂停渲染」。
- 纹理预算：每台一张 512×128（≈0.26MB）→ 8 台约 2MB，可忽略。

### 12.10 降级链（逐级，绝不阻塞）

1. `media-player.glb` 加载失败 → **程序化盒体**：`BoxGeometry` + 正面贴同一块 CanvasTexture + 一个可命中的播放按钮平面。音频功能**完全保留**，只是外观变朴素。
2. `PositionalAudio`/HRTF 不可用 → 平铺 `THREE.Audio`（仍可控可 seek）。
3. `decodeAudioData` 失败 → 元素路径（12.6 回退链）。
4. 全部失败 → 道具变成信息卡 + 「在 2D 网页播放」指引（等同 M1 行为）。

### 12.11 可选扩展（M3+，不进 M2）

- **磁带盘旋转 + 播放指示灯**（纯客户端，成本极低，观感提升大）。
- **`Button_Next` 点唱机模式**：连播房间内下一个音频文件，形成「房间电台」。
- **音量旋钮**（`Knob_Vol` 节点，旋转拖拽映射到 gain）。
- **多台录音机 = 多路声源**：配合 12.8 播音模式做「环境声装置」（如房间一角放雨声、另一角放爵士）。
- **录制**：反过来把房间声音录成 `audio` 文件（超出本规格，另行立项）。

### 12.12 验收要点

1. 摆放音频文件后，房间内出现录音机；**未摆放时 2D 与 XR 行为完全不变**。
2. 按 `Button_Play` 出声，再按暂停；屏幕进度条随时间前进，暂停即停。
3. 拖拽 `Body` 进 adjBar 不误触播放；调整中按钮失效，完成后恢复。
4. 无文件编辑权的成员**能按播放**但不能摆入/移动（403 仅在写摆位时）。
5. `filesLocked` 下不能移动但能播放。
6. 删掉 `media-player.glb` 后仍能播放（程序化盒体降级）。
7. 同房间两台录音机互不干扰；移除一台后另一台外观正常（验证共享几何未被误 dispose）。
8. 远处听感衰减、走近增强（HRTF + rolloff 生效）。

## 十三、预览三型（Preview Tiers）—— 预览问题的统一答案

> 共享文档只有两个真问题：**怎么改**（见 §五 共创编辑协议）与**怎么预览**（本节）。本节是预览的**主契约**，§一 的类型清单与 §二 的 ★ 档位都归入它的细分。

### 13.1 三型定义

| 型 | 名称 | 判定 | XR 呈现 | 2D 呈现 | 可摆入 | 典型 |
| --- | --- | --- | --- | --- | --- | --- |
| **P1** | 直接渲染型 | 我们**知道它的三维结构**，能生成几何 | 在场景世界坐标里生成物体（可走近、环视、缩放） | 缩略图（离屏渲染一帧）或引导 XR | ✅ | GLB/glTF/VRM、obj、stl、chart3d、geo3d、ifc、gcode、cad2d |
| **P2** | 预览图型 | 我们**能把它变成一张图**（本身就是图，或能渲染成图） | 一块 **plane/box** 接收该图作纹理（同现在的图片显示） | 直接显示该图 | ✅ | image、svg、markdown、csv、chart、mermaid、code、lottie、video |
| **P3** | 占位体型 | 我们**暂时不知道怎么渲染** | 系统缺省几何体（「一个文件」的样子） | 仅元数据 + 下载 | ✅ | docx、psd、odt、fbx(未支持前)、以及**任何还没有渲染器的新格式** |

三型的统一效果：**房间里摆着的东西，一律是「可以拖放的实体」**——区别只在它长什么样。房间因此永远不会有「摆不了的文件」这种空洞。

### 13.2 关键修正：三型是**能力位**，不是类型属性

**这是最容易做错的一点。** 如果把「P3」当成 `.docx` 这个类型的固有属性写进数据，将来实现了 docx 转换器，就得**回去迁移所有已有文件**才能让它们变成 P2。

正确做法：**三型在每次加载时由注册表现算**，判定规则是一个纯函数：

```
有 3D 渲染器        → P1
否则有 2D 渲染器     → P2
否则               → P3
```

推论（很重要）：**渲染器一上线，全房间历史文件立刻升级三型，零迁移、零重传**。所以类型清单里的「阶段」列不是「这个文件类型什么时候才存在」，而是**「这个类型什么时候从 P3 升到 P2/P1」**。存储层完全不知道三型的存在。

### 13.3 P1 的两个子型：静态实体 / 交互实体

你的三型里，**录音机放不进去**——它既不是「有个三维结构就直接渲染」（它还**有行为**），也不是图，也不是占位。所以 P1 内部要分：

| 子型 | 说明 | 例 |
| --- | --- | --- |
| **P1a 静态实体** | 只是几何，被看和被摆 | GLB 模型、geo3d 城市、stl 零件 |
| **P1b 交互实体** | 几何 + **行为**（点击触发、状态机、动画） | **录音机**、lottie 播放器、时间线 scrub 面板 |

「行为」是与三型**正交的第二个维度**：注册表里 `behavior` 字段声明（如 `audio-player`），缺省为空即 P1a。别把行为硬塞进三型，否则三型会失控膨胀。

### 13.4 P2 的预览图从哪来（本设计最大的一个开放问题）

P2 的前提是「能把它变成一张图」。这句话背后有**三条路线**，成本差一个数量级：

| 路线 | 做法 | 优点 | 代价 | 建议 |
| --- | --- | --- | --- | --- |
| **① 原本就是图** | 图片/SVG 直接用原图 | 零成本、零失真 | 只覆盖图类 | **无条件用** |
| **② 客户端实时渲染** | 复用 **2D 端已有的渲染器**，把它的输出画进 canvas → CanvasTexture | 永远最新、无服务端依赖、**一份渲染代码同时产出 2D 视图与 XR 纹理**（正好是既有「单 uiMesh 双 CanvasTexture」模式） | 头显里渲染开销；重格式（office）前端做不出来 | **P2 主路径** |
| **③ 服务端预生成** | 服务端无头浏览器/转换器出图，缓存 | 头显轻；能覆盖 office/psd 等前端做不了的 | **引入无头浏览器这个大依赖**，与既有「服务器只做透传与最小校验」原则冲突；还要处理 LWW 替换后的缓存失效 + 生成空窗期 | **❌ 已拍板不做**（2026-09-28） |

**已拍板的决策（2026-09-28，用户确认）**：P2 的图源**只走 ① ②**（客户端），**③ 服务端预生成明确不做**。理由：② 能免费拿到绝大多数格式（markdown/csv/chart/mermaid/svg/code 的 2D 渲染器 M1–M2 本来就要写），而 ③ 会把服务器从「透传」变成「渲染农场」，是架构级的转变——**服务器保持「只做透传与最小校验」这条既有原则，不为任何单一格式族破例**。

**推论（P3 的定位由此确定）**：`docx`/`psd` 这类**前端做不出图**的格式，**老老实实留在 P3，不做任何内容预览尝试**，而不是硬凑一个假预览。P3 的存在价值是「**让每个文件在房间里都有一个位置**」，不是「勉强显示点什么」。

### 13.4.1 P3 的最终定位（用户确认）

> **P3 = 有位置，无预览。**

| 有 | 无 |
| --- | --- |
| 缺省占位几何体（一块「写着文件名的牌子」，见 §13.6） | ❌ 任何内容渲染尝试 |
| 可摆入、可拖放、可移动/旋转/缩放/收起（与 P1/P2 同一套契约与 adjBar） | ❌ 缩略图生成 |
| 点它 → 信息卡（名称/大小/类型/更新者/时间） | ❌ 内容解析 |
| 下载（2D 网页）；有文件编辑权者可删除 | ❌ 转换尝试（转换是 §六 的独立动作，由用户/Agent 显式触发） |

「不做预览」与「可摆入」**不矛盾**：房间里放的是那块牌子本身，不是文件内容。这也是三型统一性的来源——**房间里永远没有「摆不了的文件」**。

### 13.5 P2 再分：静态图 / 活纹理

P2 内部还有一个实现上的分岔，影响更新策略：

| 子型 | 更新 | 例 |
| --- | --- | --- |
| **P2a 静态图** | 渲染一次，内容变更时重渲染 | markdown、csv、chart、code |
| **P2b 活纹理** | 持续更新（每帧或定时） | video、lottie、svg_anim、录音机屏幕 |

P2b 的纹理更新要受 §七 的性能预算约束（不在视野内/未播放时停更）。

### 13.6 P3 的缺省几何体设计（必须有区分度）

「一个盒子上写个问号」不行——房间里放 5 个未知文件就完全分不清。P3 的缺省物体必须做到**一眼能认出是哪个文件**：

| 要素 | 做法 |
| --- | --- |
| 形状 | 一个「文件/文档」感的薄板或盒体（带一点厚度，见 13.7） |
| 名称 | 把**文件名**画成纹理贴在正面（不依赖字体文件，用 canvas 画） |
| 色相 | 按 `doc_type`（或扩展名）做**稳定哈希 → 色相**，同类型同色、不同类型不同色；一眼能按色分堆 |
| 类型角标 | 角落画扩展名（如 `DOCX`） |
| 状态 | 若可转换/有文本版，角标加一个小标记（可选） |
| 点击 | 命中即显示信息卡（大小/更新者/下载/删除），**不做渲染尝试** |

### 13.7 三个实现细节（容易翻车）

1. **plane 要厚度或双面**：纯平面在 XR 里从侧面看会「消失」（背面剔除），且与占位几何易 z-fighting。建议 P2 用**薄 box**（或 `side: DoubleSide` + 微小厚度），P3 也用薄板而非零厚平面。
2. **纹理按需生成 + 分级**：**只有摆入房间的文件才生成纹理**（列表里不生成）；纹理分辨率按距离/聚焦分级（远处小图、聚焦高清）；沿用既有纹理 LRU，并设**显式上限**（建议：P2 活纹理 ≤6 张同时活跃，其余降为静态小图）。
3. **2D 端必须同步三型**：本节只写了 XR 侧。2D 抽屉必须用**同一套判定**（能直接渲染 → 预览；能出图 → 显示图；否则 → 元数据 + 下载），否则两端会漂移成两套逻辑。

### 13.8 三型共用一套摆入契约（修正 §四）

**修正**：§四 原文写「`card` 档类型 SHALL NOT 提供摆入入口」。按本节的统一效果，**P3 也可以摆入**（它就是那个缺省几何体）。摆入契约对三型完全一致：

- 同一套 `PUT .../files/{id}/placement`（位姿 = position/rotation/scale、世界坐标、底边落地、yaw-only）
- 同一套 adjBar（移动/旋转/缩放/收起/完成）
- 同一套预算制，但**P3 计更高 cost（建议 cost=2）**：占位体是「还没想好怎么处理」的东西，不该鼓励把房间当仓库塞满。房间是工作台，不是堆放区。

`mount` 字段的取值随之明确为：`entity`(P1) | `panel`(P2) | `placeholder`(P3) | `player`(P1b 特例)。

### 13.9 与 doc_type 注册表的映射

注册表不是替代三型，而是**三型的判定依据**：

| 注册表字段 | 决定 |
| --- | --- |
| `renderers.xr3d`（有无） | 能否 P1 |
| `renderers.web2d`（有无） | 能否 P2 |
| `behavior` | P1a 还是 P1b |
| `live`（true/false） | P2a 还是 P2b |
| `cost` | 摆入预算权重（P3 建议 2） |

三型 = 这三个字段的函数。**新增一种格式，只是往注册表加一条**；三型自动正确，房间里的历史文件自动升级。

### 13.10 一句话总结

> **共享文档在 3D 房间里只有三种命运：能懂它的三维结构就当实体（P1），能画出它的样子就当图片（P2），暂时不懂就当一块写着名字的牌子（P3）——但它永远是一个你能拖放的东西。**
