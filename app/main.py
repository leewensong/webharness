import asyncio
import base64
import binascii
import colorsys
import hashlib
import json
import math
import mimetypes
import os
import re
import sqlite3
import struct
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from html import escape
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile as StarletteUploadFile
from pydantic import BaseModel, Field, ValidationError

from . import auth, config, ratelimit, verify_codes
from .db import FILES_DIR, LIBRARY_DIR, UPLOADS_DIR, get_db, init_db, seed_builtin_templates
from .message_events import install_message_events

ROOT_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT_DIR / "static"
SKILL_PATH = ROOT_DIR / ".cursor" / "skills" / "webharness-api" / "SKILL.md"
GUIDE_PATH = ROOT_DIR / "docs" / "HUMAN.md"
GUIDE_EN_PATH = ROOT_DIR / "docs" / "HUMAN.en.md"
ONLINE_WINDOW = "-5 minutes"
NAME_PATTERN = r"^[\w.\-]+$"
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"}
MAX_AVATAR_BYTES = 1 * 1024 * 1024
MAX_MODEL3D_BYTES = 20 * 1024 * 1024
# 房间 3D 场景单独放宽到 50MB（带贴图的真实导出场景常超 20MB）；
# 只影响房间场景上传，附件与 3D 形象的 20MB 上限不变。
MAX_ROOM_SCENE_BYTES = 50 * 1024 * 1024
MAX_RULES_CHARS = 32000
# 长轮询单次阻塞上限：值班 Agent 用 10 分钟档挂机等待服务器唤醒（客户端半小时兜底检查存活）。
# nginx 对应 location / 的 proxy_read_timeout 必须大于该值。
MAX_LONG_POLL_SECONDS = 600
MAX_VOICE_BYTES = 10 * 1024 * 1024
MAX_STREAM_IDS = 60
# ---------- 房间封禁（v2.23）----------
# 管理员（房主/roomAgent）可把用户 ban 出房间；封禁期间无法加入、无法读取任何
# 房间数据。值为 SQLite datetime 修饰符，None = 永久；重复封禁覆盖时长。
BAN_DURATION_MODIFIERS: dict[str, str | None] = {
    "3m": "+3 minutes",
    "1h": "+1 hours",
    "24h": "+24 hours",
    "1mo": "+1 month",
    "forever": None,
}
# ---------- 共同文件（room_files）----------
# 文本类（markdown/text/svg）与二进制两档大小上限；文本类可走 JSON 直写，二进制必须 multipart
MAX_FILE_TEXT_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_FILES_PER_ROOM = 200
MAX_FILE_DESCRIPTION_CHARS = 500
# 个人资源库配额（当前为本地服务默认值，后续可搬到 config/env）。版本历史也占用
# 存储配额，避免通过无限上传新版本绕过总量限制。
MAX_LIBRARY_RESOURCES = 100
MAX_LIBRARY_TOTAL_BYTES = 500 * 1024 * 1024
WELCOME_POINTS = 1000
# 同时常驻展示（world_visible=1）的 3D 模型上限，超限开启返回 400
MAX_WORLD_MODELS = 6
# 文本类文件扩展名白名单：命中才归 kind=text（未命中的未知扩展名即使恰为 UTF-8 也归 other，
# 防止把 .docx 等二进制当文本渲染出乱码）。.md/.markdown 单列 markdown，不在此列
TEXT_FILE_EXTS = {
    ".txt", ".json", ".xml", ".yaml", ".yml", ".csv", ".tsv", ".ini", ".cfg", ".toml",
    ".log", ".html", ".htm", ".css", ".js", ".mjs", ".ts", ".py", ".rb", ".go", ".rs",
    ".java", ".c", ".h", ".cpp", ".cs", ".php", ".sh", ".sql",
    ".mermaid", ".mmd", ".drawio", ".puml", ".plantuml", ".tex", ".srt", ".vtt",
}
# 声明 mime 可信的文本类白名单（其余声明只在扩展名命中时采信）
TEXT_FILE_MIMES = {"application/json", "application/xml", "application/yaml", "application/javascript"}
MODEL_FILE_EXTS = {".glb", ".gltf", ".vrm"}
VIDEO_FILE_EXTS = {".mp4", ".webm", ".mov", ".m4v"}
VIDEO_MIME_BY_EXT = {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime", ".m4v": "video/x-m4v"}
IMAGE_MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}
# 私聊语法：消息以 @@用户名 开头（后面跟空白或整条结束）即只对该用户、
# 发送者和房主可见；连续多个 @@用户名 前缀表示多个接收者（v2.5）。
# 名字规则与用户名一致（[\w.\-]+），后跟空白/结尾避免「@@bob你好」这类连写被误解析。
WHISPER_RE = re.compile(r"^@@([\w.\-]+)(?:\s+|$)")
# @TA 与 @@ 私聊不同：只附加公开消息的提醒目标，不改变消息可见性。
MENTION_RE = re.compile(r"^@(?!@)([\w.\-]+)(?:\s+|$)")
GROUP_RE = re.compile(r"^#([\w.\-]+)(?:\s+|$)")
AUDIO_MIME_BY_EXT = {
    ".webm": "audio/webm",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
}

# 内置 3D 场景目录：房间可选的标准场景（需求 9 的 map3d，kind=builtin）。
# 几何本身由渲染端 static/xr/xr-rooms.js 的 BUILTIN_SCENES 按同一 id 程序化搭建，
# 服务器只提供可选清单与元数据，不解析场景内容。**这里的 id 集合是权威**：
# 渲染端遇到不认识的 id 会静默回退展厅环境并 console.warn，不会影响 2D。
BUILTIN_ROOM_SCENES: tuple[dict[str, Any], ...] = (
    {
        "id": "meeting",
        "name": "会议室",
        "nameEn": "Meeting room",
        "description": "长条会议桌居中，两端投影幕与白板，10 个座位",
        "descriptionEn": "Long conference table, screens at both ends, 10 seats",
        "seatCount": 10,
        # 家具活动区尺寸（米），长 × 宽
        "size": [6, 3],
    },
    {
        "id": "werewolf",
        "name": "狼人杀",
        "nameEn": "Werewolf",
        "description": "长桌两侧对坐，12 个座位",
        "descriptionEn": "Long table with seats on both sides, 12 seats",
        "seatCount": 12,
        "size": [6, 3],
    },
)
BUILTIN_SCENE_IDS = {scene["id"] for scene in BUILTIN_ROOM_SCENES}

# 内置缺省 3D 形象：账号可选的默认形象，替代「纯色胶囊」。素材是 Open Source Avatars
# 注册表 **100Avatars R1 合集的全部 100 个 CC0 模型**（可自由分发、无需署名），来源与
# 许可证见 static/avatars/CREDITS.md。**这里的 id 集合是权威**：账号用 `builtin:<id>`
# 引用（存进既有的 model3d_url 列），渲染端按约定映射成 `/static/avatars/<id>.vrm`；
# 遇到不认识的 id 回退胶囊，绝不影响 2D。file/thumbnail 给 2D 选择器用。
#
# (id, 显示名, 官方原名)：id 即 static/avatars/<id>.vrm 的文件名（取自注册表 slug）。
# 中文名只在能自然对应时给，语感特殊/玩梗的一律保留官方原名（显示名与原名相同）。
_BUILTIN_AVATAR_SPECS: tuple[tuple[str, str, str], ...] = (
    ("devil", "恶魔", "Devil"), ("polydancer", "波浪舞者", "Polydancer"),
    ("rose", "罗丝", "Rose"), ("robert", "罗伯特", "Robert"),
    ("bloody", "血面", "Bloody"), ("rabbit", "兔子", "Rabbit"),
    ("eggplant", "茄子", "Eggplant"), ("bullidan", "布利丹", "Bullidan"),
    ("mikel", "米克尔", "Mikel"), ("coolbanana", "酷香蕉", "CoolBanana"),
    ("skull", "骷髅", "Skull"), ("observer", "观察者", "Observer"),
    ("nightmare", "梦魇", "Nightmare"), ("amazonas", "亚马逊", "Amazonas"),
    ("cookieman", "饼干人", "Cookieman"), ("dinokid", "小恐龙", "DinoKid"),
    ("chad", "查德", "Chad"), ("clown", "小丑", "Clown"),
    ("chill", "凉仔", "Chill"), ("olivia", "奥莉薇亚", "Olivia"),
    ("sticker", "贴纸", "Sticker"), ("zombie", "僵尸", "Zombie"),
    ("astrodisco", "迪斯科宇航", "Astrodisco"), ("udom", "乌多姆", "Udom"),
    ("fungus", "蘑菇精", "Fungus"), ("coolchoco", "酷巧克力", "CoolChoco"),
    ("polybot", "小机器人", "Polybot"), ("ferk", "叉子", "Ferk"),
    ("erika", "艾莉卡", "Erika"), ("mummy", "木乃伊", "Mummy"),
    ("carrot", "胡萝卜", "Carrot"), ("lydia", "莉迪亚", "Lydia"),
    ("retroman", "复古人", "Retroman"), ("snowy", "雪人", "Snowy"),
    ("coffee", "咖啡杯", "Coffee"), ("ro", "罗", "Ro"),
    ("samuela", "萨缪拉", "Samuela"), ("anchor", "主播", "Anchor"),
    ("teddy", "泰迪", "Teddy"), ("saintclaus", "圣诞老人", "SaintClaus"),
    ("milk", "牛奶盒", "Milk"), ("cucumber", "黄瓜", "Cucumber"),
    ("astronaut", "宇航员", "Astronaut"), ("oldmoustache", "大胡子", "OldMoustache"),
    ("expol", "埃克斯波", "Expol"), ("ghost", "幽灵", "Ghost"),
    ("witch", "女巫", "Witch"), ("mafiossini", "黑手党", "Mafiossini"),
    ("watermelon", "西瓜", "Watermelon"), ("kate", "凯特", "Kate"),
    ("coolalien", "酷外星人", "CoolAlien"), ("chilli", "小辣椒", "Chilli"),
    ("toiletpaper", "卷纸", "ToiletPaper"), ("goodtomato", "好番茄", "GoodTomato"),
    ("xmastree", "圣诞树", "XmasTree"), ("wizzir", "巫师", "Wizzir"),
    ("skelly", "骨头人", "Skelly"), ("hotdog", "热狗", "Hotdog"),
    ("eyelids", "眼睑怪", "Eyelids"), ("froggy", "青蛙", "Froggy"),
    ("baldman", "光头佬", "Baldman"), ("dracula", "德古拉", "Dracula"),
    ("shiro", "小白", "Shiro"), ("pipe", "烟斗", "Pipe"),
    ("alwayswatching", "一直在看", "AlwaysWatching"), ("wolfman", "狼人", "Wolfman"),
    ("angry", "气鼓鼓", "Angry"), ("jennifer", "珍妮弗", "Jennifer"),
    ("muscary", "肌肉怪", "Muscary"), ("captainlobster", "龙虾船长", "CaptainLobster"),
    ("icecream", "冰淇淋", "IceCream"), ("cappy", "帽子客", "Cappy"),
    ("disturbingeyes", "惊悚之眼", "DisturbingEyes"), ("aesthetica", "美学君", "Aesthetica"),
    ("lilbro", "小兄弟", "LilBro"), ("present", "礼物盒", "Present"),
    ("jimmy", "吉米", "Jimmy"), ("kyle", "凯尔", "Kyle"),
    ("pepo", "佩波", "Pepo"), ("hugo", "雨果", "Hugo"),
    ("butter", "黄油块", "Butter"), ("horrornurse", "恐怖护士", "HorrorNurse"),
    ("scarecrow", "稻草人", "Scarecrow"), ("mushy", "蘑菇君", "Mushy"),
    ("bacondude", "培根哥", "Bacondude"), ("bigbro", "大兄弟", "BigBro"),
    ("avocado", "牛油果", "Avocado"), ("cactusboy", "仙人掌小子", "CactusBoy"),
    ("david", "大卫", "David"), ("candycane", "拐杖糖", "CandyCane"),
    ("franky", "弗兰奇", "Franky"), ("wirefriend", "电线朋友", "WireFriend"),
    ("crimsom", "克霖森", "Crimsom"), ("confirmed", "确认君", "Confirmed"),
    ("wambo", "万博", "Wambo"), ("toothpaste", "牙膏", "Toothpaste"),
    ("weirdflexbutok", "奇怪炫耀", "WeirdFlexButOk"), ("cubiq", "立方体", "Cubiq"),
    ("mint", "薄荷", "Mint"), ("pumpkin", "南瓜", "Pumpkin"),
)
BUILTIN_AVATARS: tuple[dict[str, Any], ...] = tuple(
    {
        "id": _id,
        "name": _zh,
        "nameEn": _en,
        "description": _en,          # 卡片 tooltip 显示官方原名
        "descriptionEn": _en,
        "file": f"/static/avatars/{_id}.vrm",
        "thumbnail": f"/static/avatars/{_id}.webp",
        # 100 个实测均为 VRM0，含 6 个表情预设与人形骨骼 → 表情与骨骼动画都可用
        "arkit": True,
        "humanoid": True,
        "source": "100Avatars R1 (CC0)",
    }
    for _id, _zh, _en in _BUILTIN_AVATAR_SPECS
)
BUILTIN_AVATAR_BY_ID = {avatar["id"]: avatar for avatar in BUILTIN_AVATARS}
# 账号 model3d_url 里内置形象引用的前缀：`builtin:<id>`
BUILTIN_AVATAR_PREFIX = "builtin:"

_main_loop: asyncio.AbstractEventLoop | None = None
_room_events: dict[int, asyncio.Event] = {}


def _room_event(room_id: int) -> asyncio.Event:
    ev = _room_events.get(room_id)
    if ev is None:
        ev = asyncio.Event()
        _room_events[room_id] = ev
    return ev


def notify_room(room_id: int) -> None:
    """唤醒正在长轮询该房间的等待者。可从线程池里的同步路由调用。"""
    loop = _main_loop
    if loop is None:
        return

    def _fire() -> None:
        old = _room_events.pop(room_id, None)
        _room_events[room_id] = asyncio.Event()
        if old is not None:
            old.set()

    loop.call_soon_threadsafe(_fire)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _main_loop
    _main_loop = asyncio.get_running_loop()
    init_db()
    seed_builtin_templates()
    for warning in config.startup_warnings():
        print(f"[webharness] 警告：{warning}", flush=True)
    yield
    _main_loop = None


app = FastAPI(
    title="WebHarness.Chat @FXG",
    version="2.31.2",
    description="人类 Web UI 在 `/`；人类说明书在 `/guide`（`?lang=en` 英文）；Agent 用短 HTTP API（密钥对登录），说明书在 `/skill.md`。文本消息支持流式写入，正文富文本渲染：Markdown / Mermaid 图 / ```svg 矢量图 / ```chart 数据图 / ```a2ui 声明式数据面板（A2UI 协议，数据与组件分离，样式归渲染端）。Web UI 支持浏览器语音输入（ASR）与语音朗读（TTS）、中英双语（右上角「中 / E」）。账号支持 2D 头像（≤1MB，缺省自动生成）与可选 3D 形象（≤20MB 的 GLB/GLTF 或外链 URL，可标记 ARKit 52 表情与 Unity Humanoid 全身骨骼）。房间支持 `rules` 规则文本与 `roomAgent` 授权 Agent（roomAgent 可代房主治理房间：改房间设置/全体禁言/rules、成员禁言等权限、私聊白黑名单、封禁成员（`POST/GET/DELETE /api/rooms/{room}/bans`，档位 3m/1h/24h/1mo/forever；被封禁者无法加入房间、无法读取任何房间数据，房主与 roomAgent 不可被封禁），并可见全部私聊与完整历史）。私聊：消息以 `@@用户名`（可连续多个）开头，只对发送者、接收者、房主可见；Web UI 点在线用户「加入私聊」并在输入框上方显示 chips。命名群组（v2.8）：房主/roomAgent 用 `POST /api/rooms/{room}/groups` 登记（如狼人群），成员发 `#群名 内容` 自动展开为发给全组的私聊；群组成员名单对非成员保密。房间模板（v2.9）：`GET/POST /api/room-templates` 等接口管理模板（如内置「狼人杀 9 人局」，rules 文本 + 可下载的裁判脚本附件）；建房时带 `template` 名会复制模板 rules 进新房间，房间详情回显 `template`/`templateScript`，房主选定的 Room Agent 据此下载脚本在本地执行（也可用本地脚本）；模板脚本另有免登录静态下载 `GET /scripts/templates/{模板名}`（rules 里写的就是这个地址），rules 文本支持 `{{BASE_URL}}` 占位符（返回时按请求来源填充）。消息支持引用回复（`replyTo`，灰色小字引用块可跳回原消息）、撤回本房间最后一条消息（不限时长，只要之后没有新消息；`DELETE .../messages/{id}`，所有客户端移除）与语音消息（`POST .../voice`，音频 + ASR 文本，渲染文字并可播放原声）。房间 3D 场景（v2.12）：房间可携带 `scene`（内置会议室 10 座 / 狼人杀 12 座，或上传的自包含 GLB（≤50MB），或外链 URL）；`GET /api/room-scenes` 列出内置场景，建房与 `PATCH /api/rooms/{room}` 用 `{kind: builtin | url | none}` 设定，`POST/DELETE /api/rooms/{room}/scene` 上传与清除，`GET /api/rooms/{room}/scene` 下载上传件（仅成员）。服务器只做透传与最小校验，内置场景的几何由 3D 渲染端按 id 程序化搭建；成员形象按场景提供的推荐座位就座，无场景时仍是原来的展厅环境。内置缺省 3D 形象（v2.13，v2.22 起扩到 100 个）：`GET /api/avatar-models` 列出内置形象（**Open Source Avatars「100Avatars R1」合集的全部 100 个 CC0 VRM**，含缩略图与表情/骨骼能力位；2D 选择器支持搜索，卡片区限高滚动）；账号用 `PUT /api/me/model3d` 传 url=`builtin:<id>` 选用（`as=` 可代 Agent 设置），也可继续上传自己的 GLB/VRM 或填外链；模型本体是静态资源 `static/avatars/`，来源、许可证与**入库前所做的压缩**（删未引用的形变靶＝无损 + 贴图降采样＝有损）见 `static/avatars/CREDITS.md`。Agent 创建支持可选的同名房间：人类在 Web UI 勾选后，`POST /api/agents` 的 `createRoom:true` 会原子创建同名私有房间并把新 Agent 设为 roomAgent，响应含 `room`；超级管理员使用 `/api/admin` 与 `/api/admin/suggestions` 查看、筛选、处理建议，Web UI 侧栏提供平台管理入口。房间共同文件（v2.18）：每房一份共享文件列表（`GET/POST /api/rooms/{room}/files` 等，LWW 只留最新版、`sinceRevision`+`wait` 长轮询、`baseUpdatedAt` 乐观锁、上限 200 个/房）；8 类 kind（markdown/text/svg/image/video/model/audio/other）按魔数判定，2D 网页抽屉与 3D 空间面板都可上传/编辑/预览；3D 模型可 `PUT .../files/{id}/placement` 摆入房间常驻展示（世界坐标系、显式 scale、同时 ≤6 个、`visible:false` 保留位姿），XR 端支持拖拽/摇杆调整与头显键盘编辑；权限 = 成员 `canEditFiles` + 房间 `filesLocked`（治理者恒豁免），归档房间文件只读。内容路由约定：一次性表达走聊天富文本，会迭代内容进共同文件，3D 内容（GLB/GLTF/VRM）一律共同文件。语音文本补写（v2.21）：`PATCH /api/rooms/{room}/voice/{messageId}/text` 让语音作者或其名下 Agent 为空文本语音补写本地 ASR 转写文本（识别不出写「（空）」；已有正文 409 不可覆盖、不能带 @@/# 前缀），2D 端作者也可在自己空文本语音的消息菜单手动补写。房间列表管理（v2.24）：非房主可用 `PUT /api/rooms/{room}/hidden` 把别人创建的房间从自己的「我的」列表移除（纯本人视图过滤，房间与聊天记录原样保留，房主/Agent 主人不可移除、只能归档；重新创建或加入该房间会自动恢复），Web UI 在「我的」列表的房间行悬停时显示 ✕。手机短信 / 邮箱验证码（v2.25）：`GET /api/auth/channels` 公开通道可用性（前端据此隐藏验证码入口；都不配则自动降级回「用户名 + 密码」）；`POST /api/auth/send-code` 发码（短信走阿里云号码认证服务 PNVS，码由阿里云生成与核验、本服务不落码；邮箱码由本服务生成、库里只存 PBKDF2 哈希。同目标 60 秒重发间隔、300 秒有效、每码最多试 5 次、核验通过即写 `verified_at`，同一码不可重放且用途必须一致）。人类注册可带 `phone`+`phoneCode` 或 `email`+`emailCode`（任一通道可用时二选一必填）；`POST /api/login` 额外支持 `{identifier, code}` 免密登录（`identifier` 按 手机→邮箱 解析，不接受用户名）；`POST /api/auth/reset-password` 用验证码重置密码；`PUT /api/me/password` 改密码；`PUT /api/me/contacts` 与 `POST /api/me/contacts/unbind` 绑定/换绑/解绑（换绑需新目标验证码 + 当前密码）。手机号与邮箱只在自己 `/api/me` 里以掩码返回（`139****0001` / `a***@qq.com`），不进在线成员、房间成员、Agent 列表等任何他人可见的响应；改密与重置密码都会让 `token_epoch` +1，使所有旧 token 立即失效（本人当前会话由接口补发的新 token 接续）。房间内 3D 位姿流（v2.26）：`POST /api/rooms/{room}/presence` 上报（头/身体 + 可选双手 + 可选 state）、`/presence/leave` 离开、`GET /presence` 全量快照（返回 `logId` 作增量游标）、`GET /presence/delta` 增量。增量支持 `fmt=bin` 返回**二进制脏位帧**（u32 成员 id + kind + 脏位掩码 + 按需字段；位置量化到厘米、角度到 int16、四元数到 int16），并**按请求者到各成员的水平距离分级**：<5m 全量（含双手与状态）、5–15m 位置+朝向、>15m 仅位置；静止成员若没有脏字段则一个字节都不发。游标用 `sinceId`（增量日志 id，单调递增；时间戳游标会漏同一毫秒的事件），返回 `X-Presence-Id`/`X-Presence-Reset`。`hold`（毫秒）为服务端节流：不足则等满再返回，客户端「返回就再发」即得稳定 tick（10Hz 传 100），避免「谁写入就唤醒谁」在高频下的惊群。预留脏位 16/32 给全身骨骼与 ARKit52 面部。Agent 可在上报时自报**能力档** `level`（1=只报位姿；2=再加双手；3=再加全身骨骼与表情，骨骼块尚未实现、表情走 state 已可用），服务端按档强制（声明 level 1 却带 hands 直接 400），档位落在 `room_presence.level` 并随全量快照透出，不传则按载荷推断（有 hands 记 2，否则 1）。房间列表排序（v2.27）：「我的」列表缺省按「我参与的更新时间」降序——我在房里看到的最新一条**可见**消息的时间（别人之间我看不到的私聊不算；房主与 roomAgent 照旧可见全部私聊）。该值只增不减、随我进房读消息或自己发言而推进，所以不进房间它就一直变旧、排名往下掉，进去看一眼但没人说话也不会提高；从没进过的房间按创建时间兜底。Agent 主人与房主同级（v2.28）：房间由 Agent 创建时，该 Agent 的人类主人（agent.owner_id）取得与房主/roomAgent 同级的治理权——房间详情新增 `canManage` 字段；可改房间设置（改名/密码/可见性/全体禁言/rules/共同文件锁）、成员权限、私聊白黑名单、封禁，可见全部私聊与完整历史，并与房主、roomAgent 一样不可被限制或封禁。归档权维持原样：房主或 Agent 主人可归档，roomAgent 不可。摆入模型自转（v2.28.2）：placement 的 pose 新增 `spin`（绕 Y 自转角速度，度/秒，负=反向，|spin|≤360）；不带 spin 字段时保留原值，显式传 0 关闭；XR 客户端每帧推进自转（调整拖拽中暂停）。",
    lifespan=lifespan,
)


@app.middleware("http")
async def _static_no_cache(request: Request, call_next):
    """页面与代码资产强制回源校验（etag/304）：静态文件只带 etag 时浏览器会启发式
    缓存、发版后继续用旧 JS（如 3D 模块 import 无版本参数）。文件都小，代价可忽略。"""
    resp = await call_next(request)
    p = request.url.path
    if p == "/" or p.startswith("/static") or p.startswith("/guide"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=32, pattern=NAME_PATTERN)
    password: str = Field(min_length=4, max_length=128)
    # 可选 2D 头像，data URL（data:image/jpeg;base64,...），解码后 ≤1MB；留空则用缺省头像
    avatar: str | None = Field(default=None, max_length=2_000_000)
    # 手机 / 邮箱（二选一或都填）+ 各自验证码；是否强制见 config.verify_required()
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=254)
    phoneCode: str | None = Field(default=None, max_length=32)
    emailCode: str | None = Field(default=None, max_length=32)


class LoginRequest(BaseModel):
    """两种登录方式二选一：用户名 + 密码，或 手机/邮箱 + 验证码。"""

    username: str | None = Field(default=None, max_length=254)
    password: str | None = Field(default=None, max_length=128)
    identifier: str | None = Field(default=None, max_length=254)
    code: str | None = Field(default=None, max_length=32)


class SendCodeRequest(BaseModel):
    channel: Literal["phone", "email"]
    target: str = Field(min_length=3, max_length=254)
    purpose: Literal["register", "login", "bind", "reset"]


class ContactUpdate(BaseModel):
    channel: Literal["phone", "email"]
    target: str = Field(min_length=3, max_length=254)
    code: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class PasswordChange(BaseModel):
    oldPassword: str = Field(min_length=1, max_length=128)
    newPassword: str = Field(min_length=4, max_length=128)


class PasswordReset(BaseModel):
    identifier: str = Field(min_length=3, max_length=254)
    code: str = Field(min_length=1, max_length=32)
    newPassword: str = Field(min_length=4, max_length=128)


class ContactUnbind(BaseModel):
    channel: Literal["phone", "email"]
    password: str = Field(min_length=1, max_length=128)


class AgentCreate(BaseModel):
    username: str = Field(min_length=2, max_length=32, pattern=NAME_PATTERN)
    publicKey: str = Field(min_length=1, max_length=4096)
    avatar: str | None = Field(default=None, max_length=2_000_000)
    model3dUrl: str | None = Field(default=None, max_length=2048)
    # 3D 形象标准标记：ARKit 52 = 面部 blendshape；Humanoid = Unity 人形全身骨骼
    model3dArkit: bool = False
    model3dHumanoid: bool = False
    # 创建 Agent 时可原子创建同名房间，并把新 Agent 设为 roomAgent。
    createRoom: bool = False


class Model3dUpdate(BaseModel):
    url: str | None = Field(default=None, max_length=2048)
    arkit: bool | None = None
    humanoid: bool | None = None


class AgentUpdate(BaseModel):
    username: str | None = Field(default=None, min_length=2, max_length=32, pattern=NAME_PATTERN)
    publicKey: str | None = Field(default=None, min_length=1, max_length=4096)
    status: Literal["active", "disabled"] | None = None


class ChallengeRequest(BaseModel):
    username: str


class AgentLoginRequest(BaseModel):
    username: str
    signature: str


class RoomSceneInput(BaseModel):
    """建房/改房时传入的 3D 场景引用（rooms.map3d）。接受「选内置」「填外链」
    「选资源库已过审模型」与「清空」四种；v2.32 起不再支持直接上传。"""
    kind: Literal["builtin", "url", "library", "none"]
    id: str | None = Field(default=None, max_length=64)
    url: str | None = Field(default=None, max_length=2048)


class RoomRequest(BaseModel):
    # 新客户端优先使用 roomId/roomKey；roomName 保留给旧客户端兼容。
    roomId: int | None = Field(default=None, ge=1)
    roomKey: str | None = Field(default=None, min_length=3, max_length=160)
    roomName: str | None = Field(default=None, min_length=1, max_length=64, pattern=NAME_PATTERN)
    password: str | None = Field(default=None, max_length=128)
    visibility: Literal["private", "public"] | None = None
    rules: str | None = Field(default=None, max_length=MAX_RULES_CHARS)
    roomAgent: str | None = Field(default=None, max_length=32)
    # 房间模板名：新房间复制模板 rules（显式传 rules 时以 rules 为准），并记录来源模板
    template: str | None = Field(default=None, max_length=32, pattern=r"^[\w.\-]+$")
    # 3D 场景：不传表示无场景（渲染端沿用展厅环境）
    scene: RoomSceneInput | None = None


class RoomUpdate(BaseModel):
    roomName: str | None = Field(default=None, min_length=1, max_length=64, pattern=NAME_PATTERN)
    password: str | None = Field(default=None, max_length=128)
    visibility: Literal["private", "public"] | None = None
    muted: bool | None = None
    # 共同文件锁定：开启后除治理者外任何成员不可写文件（与 muted 并列）
    filesLocked: bool | None = None
    rules: str | None = Field(default=None, max_length=MAX_RULES_CHARS)
    # 传空字符串表示清空 room agent；不传（None）表示不改
    roomAgent: str | None = Field(default=None, max_length=32)
    # 3D 场景：不传（字段缺席）表示不改；显式传 null 表示清除场景，回到展厅环境
    scene: RoomSceneInput | None = None


class FriendRequestCreate(BaseModel):
    username: str = Field(min_length=2, max_length=32, pattern=NAME_PATTERN)


class InviteCreate(BaseModel):
    username: str = Field(min_length=2, max_length=32, pattern=NAME_PATTERN)


class RoomAgentJoinRequest(BaseModel):
    agentUsername: str = Field(min_length=2, max_length=32, pattern=NAME_PATTERN)


class LibraryResourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=500)


class ResourceReviewDecision(BaseModel):
    decision: Literal["approved", "rejected"]
    note: str = Field(default="", max_length=2000)


class ResourceListingCreate(BaseModel):
    pricePoints: int = Field(ge=0, le=2_000_000_000)


class ResourceListingUpdate(BaseModel):
    pricePoints: int | None = Field(default=None, ge=0, le=2_000_000_000)
    listed: bool | None = None


class LibraryImportRequest(BaseModel):
    resourceId: int = Field(ge=1)
    resourceVersionId: int = Field(ge=1)
    listingId: int | None = Field(default=None, ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    idempotencyKey: str | None = Field(default=None, min_length=1, max_length=128)


class PointsCampaignGrant(BaseModel):
    username: str = Field(min_length=2, max_length=32, pattern=NAME_PATTERN)
    campaignKey: str = Field(min_length=3, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    amount: int = Field(ge=1, le=2_000_000_000)
    reason: str = Field(min_length=1, max_length=200)


def _grant_points(
    conn,
    *,
    user_id: int,
    amount: int,
    transaction_type: Literal["signup_grant", "campaign_grant"],
    idempotency_key: str,
    reason: str,
    campaign_key: str | None = None,
) -> bool:
    """Credit one human wallet and its immutable ledger row in the caller's transaction."""
    metadata = {"reason": reason}
    if campaign_key is not None:
        metadata["campaignKey"] = campaign_key
    metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    inserted = conn.execute(
        """
        INSERT INTO point_transactions (
            idempotency_key, transaction_type, payer_user_id, payee_user_id,
            amount, status, metadata
        )
        VALUES (?, ?, ?, ?, ?, 'committed', ?)
        ON CONFLICT(idempotency_key) DO NOTHING
        """,
        (idempotency_key, transaction_type, user_id, user_id, amount, metadata_json),
    )
    if inserted.rowcount == 0:
        prior = conn.execute(
            "SELECT transaction_type, payer_user_id, payee_user_id, amount, metadata FROM point_transactions WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if not prior or (
            prior["transaction_type"] != transaction_type
            or prior["payer_user_id"] != user_id
            or prior["payee_user_id"] != user_id
            or prior["amount"] != amount
            or prior["metadata"] != metadata_json
        ):
            raise _friend_error("points_idempotency_conflict", "相同幂等键已用于不同积分发放", 409)
        return False
    conn.execute(
        """
        INSERT INTO point_accounts (user_id, balance) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            balance = point_accounts.balance + excluded.balance,
            updated_at = datetime('now')
        """,
        (user_id, amount),
    )
    return True


class PermissionUpdate(BaseModel):
    canSpeak: bool | None = None
    canUpload: bool | None = None
    canViewHistory: bool | None = None
    canEditFiles: bool | None = None


class BanCreate(BaseModel):
    """封禁用户：时长只能从固定档位里选（服务端白名单，杜绝任意时长）。"""
    username: str = Field(min_length=1, max_length=32)
    duration: Literal["3m", "1h", "24h", "1mo", "forever"]


class FileCreate(BaseModel):
    """文本类共同文件 JSON 直写（二进制走 multipart）。"""
    name: str = Field(min_length=1, max_length=256)
    content: str
    description: str | None = Field(default=None, max_length=MAX_FILE_DESCRIPTION_CHARS)


class FileReplace(BaseModel):
    """整体替换（JSON 直写文本类）；baseUpdatedAt 不符返回 409。"""
    content: str
    baseUpdatedAt: str | None = Field(default=None, max_length=40)


class FileMetaUpdate(BaseModel):
    """重命名 / 改描述。"""
    name: str | None = Field(default=None, min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=MAX_FILE_DESCRIPTION_CHARS)


class PlacementUpdate(BaseModel):
    """3D 世界摆放（需求 10）。visible=true 时 position 必填；rotation/scale 缺省不重置为
    单位阵，而是服务端补默认值（首摆语义）；visible=false 关闭显示但保留位姿。"""
    visible: bool
    position: list[float] | None = None
    rotation: list[float] | None = None
    scale: list[float] | None = None
    spin: float | None = None   # 绕 Y 自转（度/秒，负=反向）；缺省保留原值，显式 0 关闭


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=64000)
    # 引用回复：被引用消息的 id（必须是本房间、未撤回、对发送者可见的消息）
    replyTo: int | None = None


class StreamStart(BaseModel):
    content: str = Field(default="", max_length=64000)
    replyTo: int | None = None


class StreamPatch(BaseModel):
    delta: str | None = Field(default=None, max_length=64000)
    content: str | None = Field(default=None, max_length=64000)
    done: bool = False


class VoiceTextPatch(BaseModel):
    text: str = Field(min_length=1, max_length=64000)


SuggestionCategory = Literal["bug", "skill", "feature", "other"]
SuggestionStatus = Literal["new", "reviewing", "planned", "resolved", "rejected"]
SUGGESTION_STATUSES = ("new", "reviewing", "planned", "resolved", "rejected")
ADMIN_CAPABILITIES = ("suggestions.read", "suggestions.manage")


class SuggestionCreate(BaseModel):
    content: str = Field(min_length=1, max_length=5000)
    contact: str | None = Field(default=None, max_length=200)
    category: SuggestionCategory = "other"


class SuggestionUpdate(BaseModel):
    status: SuggestionStatus | None = None
    adminNote: str | None = Field(default=None, max_length=5000)


class WhisperRuleCreate(BaseModel):
    listType: Literal["allow", "deny"]
    # 发送者/接受者：用户名或 *（所有人）
    sender: str = Field(min_length=1, max_length=32, pattern=r"^(?:[\w.\-]+|\*)$")
    receiver: str = Field(min_length=1, max_length=32, pattern=r"^(?:[\w.\-]+|\*)$")
    priority: int = Field(default=0, ge=-1000, le=1000)


class GroupCreate(BaseModel):
    # 房间命名群组：成员发 `#群名 内容` 自动展开为对全组的私聊（如狼人杀的狼人群）
    name: str = Field(min_length=1, max_length=32, pattern=r"^[\w.\-]+$")
    members: list[str] = Field(default_factory=list, max_length=64)


class GroupUpdate(BaseModel):
    members: list[str] = Field(default_factory=list, max_length=64)


class TemplateCreate(BaseModel):
    # 房间模板：建房时按 name 复制 rules，脚本附件供 Room Agent 下载到本地执行
    name: str = Field(min_length=1, max_length=32, pattern=r"^[\w.\-]+$")
    title: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=2000)
    rules: str = Field(default="", max_length=MAX_RULES_CHARS)
    params: dict[str, Any] = Field(default_factory=dict)
    scriptName: str | None = Field(default=None, max_length=128, pattern=r"^[\w.\-]+$")
    scriptBase64: str | None = Field(default=None, max_length=7_000_000)


class TemplateUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=2000)
    rules: str | None = Field(default=None, max_length=MAX_RULES_CHARS)
    params: dict[str, Any] | None = None
    scriptName: str | None = Field(default=None, max_length=128, pattern=r"^[\w.\-]+$")
    scriptBase64: str | None = Field(default=None, max_length=7_000_000)


def require_user(authorization: Annotated[str | None, Header(alias="Authorization")] = None):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 token，请先登录")
    user = auth.parse_token(authorization.removeprefix("Bearer ").strip())
    if not user:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    # 每次请求确认账号仍可用、且没被改密/重置密码踢下线：token 里带着签发时的
    # token_epoch，与库里对不上即失效（也让禁用账号的旧 token 立刻作废）。
    with get_db() as conn:
        row = conn.execute(
            "SELECT username, kind, owner_id, status, token_epoch, is_superadmin FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
    if not row or row["status"] != "active":
        raise HTTPException(status_code=401, detail="账号不可用")
    if int(row["token_epoch"] or 0) != user["epoch"]:
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录")
    # 权限与身份均以当前库记录为准，不信任 token 中的角色快照；撤权无需重新登录。
    user.update(username=row["username"], kind=row["kind"], owner_id=row["owner_id"],
                isSuperadmin=row["kind"] == "human" and bool(row["is_superadmin"]))
    return user


CurrentUser = Annotated[dict, Depends(require_user)]


def require_human(user: CurrentUser):
    if user["kind"] != "human":
        raise HTTPException(status_code=403, detail="仅人类用户可管理 Agent")
    return user


HumanUser = Annotated[dict, Depends(require_human)]


def require_superadmin(user: CurrentUser, response: Response):
    """所有平台管理 API 的统一守卫；Agent 不继承主人的平台权限。"""
    if not user["isSuperadmin"]:
        raise HTTPException(status_code=403, detail="仅超级管理员可访问平台管理功能")
    response.headers["Cache-Control"] = "no-store"
    return user


SuperAdmin = Annotated[dict, Depends(require_superadmin)]


def _effective_human_id(user: dict) -> int:
    """Agent 与主人共享好友/资源命名空间；Agent 本身仍保留审计身份。"""
    if user.get("kind") == "agent" and user.get("owner_id"):
        return int(user["owner_id"])
    return int(user["id"])


@app.exception_handler(verify_codes.VerifyError)
def _verify_error_handler(_: Request, exc: verify_codes.VerifyError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


# ---------- 头像与 3D 形象 ----------

_DATA_URL_RE = re.compile(r"^data:([\w.+-]+/[\w.+-]+);base64,(.*)$", re.DOTALL)


def _avatar_url(username: str, version: str | None) -> str:
    return f"/api/users/{username}/avatar?v={quote(str(version or 0), safe='')}"


def _model3d_file_url(username: str, version: str | None) -> str:
    return f"/api/users/{username}/model3d?v={quote(str(version or 0), safe='')}"


def _builtin_avatar_id(value: str | None) -> str | None:
    """解析 model3d_url 里的内置形象引用 `builtin:<id>`。
    不是这个前缀返回 None（照旧当外链处理）；是前缀但 id 不在册则 400——
    否则会写下一个渲染端永远认不出、只能回退胶囊的引用。"""
    if not value or not value.startswith(BUILTIN_AVATAR_PREFIX):
        return None
    avatar_id = value[len(BUILTIN_AVATAR_PREFIX):].strip()
    if avatar_id not in BUILTIN_AVATAR_BY_ID:
        raise HTTPException(status_code=400, detail="未知的内置 3D 形象")
    return avatar_id


# model3d_url / rooms.map3d 里的资源库引用前缀：`library:<resourceId>`。
# 只有 review_status=approved 的资源能被引用（v2.32 起个人形象与房间场景
# 不再支持直接上传，自定义模型必须先传资源库并过审）。
LIBRARY_REF_PREFIX = "library:"


def _library_ref_id(value: str | None) -> int | None:
    """解析 `library:<resourceId>` 引用；不是这个前缀返回 None，是前缀但
    id 不是正整数则 400（同 builtin 的「写不进去的引用直接拒绝」原则）。"""
    if not value or not value.startswith(LIBRARY_REF_PREFIX):
        return None
    raw = value[len(LIBRARY_REF_PREFIX):].strip()
    if not raw.isdigit() or int(raw) <= 0:
        raise HTTPException(status_code=400, detail="资源库引用必须是 library:<资源ID>")
    return int(raw)


def _default_avatar_svg(username: str) -> bytes:
    """按用户名确定性生成缺省头像：随机感配色的圆角方块 + 用户名首字符。"""
    digest = hashlib.sha256(username.lower().encode("utf-8")).digest()
    hue = digest[0] / 255.0
    r, g, b = colorsys.hls_to_rgb(hue, 0.42, 0.55)
    bg = "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))
    tr, tg, tb = colorsys.hls_to_rgb(hue, 0.92, 0.75)
    fg = "#%02x%02x%02x" % (round(tr * 255), round(tg * 255), round(tb * 255))
    letter = escape(username.strip()[:1].upper() or "?")
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" width="128" height="128">'
        f'<rect width="128" height="128" rx="28" fill="{bg}"/>'
        '<text x="64" y="66" text-anchor="middle" dominant-baseline="central" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif" '
        f'font-size="64" font-weight="600" fill="{fg}">{letter}</text>'
        "</svg>"
    )
    return svg.encode("utf-8")


def _decode_data_url(value: str) -> tuple[bytes, str]:
    """解析 data URL，返回 (原始字节, mime)。"""
    match = _DATA_URL_RE.match(value.strip())
    if not match:
        raise HTTPException(status_code=400, detail="头像格式无效，需要 data:image/jpeg;base64,... 形式")
    mime, payload = match.group(1).lower(), match.group(2)
    try:
        data = base64.b64decode(re.sub(r"\s+", "", payload), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="头像 base64 解码失败") from exc
    return data, mime


def _validate_avatar_bytes(data: bytes, declared_mime: str | None) -> str:
    """校验头像字节，返回规范化 mime（image/jpeg 或 image/png）。"""
    if len(data) > MAX_AVATAR_BYTES:
        raise HTTPException(status_code=413, detail="头像超过 1MB 上限")
    detected = None
    if data.startswith(b"\xff\xd8\xff"):
        detected = "image/jpeg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = "image/png"
    if detected is None:
        raise HTTPException(status_code=400, detail="头像必须是 JPG 或 PNG 图片")
    declared = (declared_mime or "").split(";")[0].strip().lower()
    # 只在与已知图片类型冲突时报错；octet-stream 之类的笼统声明交给魔数判断
    if declared.startswith("image/") and declared != detected:
        raise HTTPException(status_code=400, detail="头像内容与声明的图片格式不一致")
    return detected


def _glb_looks_vrm(data: bytes) -> bool:
    """VRM 即 GLB 容器（魔数同为 glTF），JSON chunk 开头带 VRM/VRMC_vrm 扩展。
    仅用于细化 mime 标识（记录用途），存储与加载路径不变（GLTFLoader 不看 Content-Type）。"""
    head = data[12:4112]  # 跳过 12B GLB header，覆盖 JSON chunk 开头
    return b'"VRM"' in head or b'"VRMC_vrm"' in head


def _gltf_mime(data: bytes) -> str | None:
    """按魔数识别 glTF 家族，返回 mime；不是 glTF 家族返回 None。"""
    if data.startswith(b"glTF"):
        return "model/vrm" if _glb_looks_vrm(data) else "model/gltf-binary"
    if data.lstrip()[:1] == b"{":
        return "model/gltf+json"
    return None


def _validate_model3d_bytes(data: bytes) -> str:
    """校验 3D 模型字节，返回 mime（GLB / GLTF / VRM）。VRM 返回 model/vrm 仅为
    细化提示与记录，兼容现状：已有数据不迁移，加载端按同一 glTF 路径处理。"""
    if len(data) > MAX_MODEL3D_BYTES:
        raise HTTPException(status_code=413, detail="3D 模型超过 20MB 上限")
    mime = _gltf_mime(data)
    if mime is None:
        raise HTTPException(status_code=400, detail="3D 模型必须是 GLB（glTF 二进制）、GLTF（JSON）或 VRM 文件")
    return mime


def _validate_room_scene_bytes(data: bytes) -> str:
    """房间场景与 3D 形象走同一套 glTF 魔数校验，仅上限放宽到 50MB。
    额外拒绝 .gltf（JSON）：它引用外部 .bin/贴图，单文件上传后 GLTFLoader 取不到
    依赖，用户只会得到无从下手的「加载失败」，不如在这里明确挡掉。"""
    if len(data) > MAX_ROOM_SCENE_BYTES:
        raise HTTPException(status_code=413, detail="3D 场景超过 50MB 上限")
    mime = _gltf_mime(data)
    if mime is None:
        raise HTTPException(status_code=400, detail="3D 场景必须是 GLB（glTF 二进制）或 VRM 文件")
    if mime == "model/gltf+json":
        raise HTTPException(
            status_code=400,
            detail="场景必须是自包含的 GLB；.gltf 会引用外部资源，无法单文件上传",
        )
    return mime


def _room_scene_file_url(room_name: str, version: str | None) -> str:
    return f"/api/rooms/{quote(room_name, safe='')}/scene?v={quote(str(version or 0), safe='')}"


def _room_scene_descriptor(room) -> dict[str, str] | None:
    """房间场景描述符（rooms.map3d），返回给客户端的形状：kind=builtin/file/url/library。
    kind=file 的 url 一律由服务器按当前版本号现算，不采信库里存的 URL；
    kind=library 改写成带版本号的房间场景代理地址（下载权限在该接口按房间成员
    判定），渲染端只当它是个普通 url，无需认识资源库。
    描述符残缺或内置 id 已下线时返回 None——渲染端据此回退展厅环境。"""
    raw = _row_get(room, "map3d")
    if not raw:
        return None
    try:
        desc = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(desc, dict):
        return None
    kind = desc.get("kind")
    if kind == "builtin" and desc.get("id") in BUILTIN_SCENE_IDS:
        return {"kind": "builtin", "id": desc["id"]}
    if kind == "file":
        return {"kind": "file", "url": _room_scene_file_url(room["name"], _row_get(room, "scene_updated_at"))}
    if kind == "library":
        try:
            library_id = int(desc.get("id") or 0)
        except (TypeError, ValueError):
            return None
        if library_id <= 0:
            return None
        return {"kind": "library", "id": str(library_id), "url": _room_scene_file_url(room["name"], _row_get(room, "scene_updated_at"))}
    if kind == "url":
        url = str(desc.get("url") or "").strip()
        return {"kind": "url", "url": url} if url else None
    return None


def _scene_descriptor_json(scene: "RoomSceneInput") -> str | None:
    """把客户端的场景引用规整成待存进 rooms.map3d 的 JSON 文本；kind=none 返回 None
    （清空）。kind=library 的 id 存资源 id 字符串，读取侧按 int 解析。"""
    if scene.kind == "none":
        return None
    if scene.kind == "builtin":
        if scene.id not in BUILTIN_SCENE_IDS:
            raise HTTPException(status_code=400, detail="未知的内置 3D 场景")
        return json.dumps({"kind": "builtin", "id": scene.id}, ensure_ascii=False)
    if scene.kind == "library":
        library_id = _library_ref_id(f"{LIBRARY_REF_PREFIX}{scene.id or ''}")
        if library_id is None:
            raise HTTPException(status_code=400, detail="资源库场景需要有效的资源 ID")
        return json.dumps({"kind": "library", "id": library_id}, ensure_ascii=False)
    url = (scene.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="外部 3D 场景需要 url")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="外部 3D 场景 url 必须是 http/https")
    return json.dumps({"kind": "url", "url": url}, ensure_ascii=False)


def _get_user_by_id(conn, user_id: int):
    return conn.execute(
        f"""
        SELECT id, {PROFILE_COLUMNS}
        FROM users WHERE id = ?
        """,
        (user_id,),
    ).fetchone()


def _resolve_profile_target(conn, user: dict, as_username: str | None):
    """形象设置的目标账号：默认自己；as= 指定时必须是当前用户名下的 Agent。"""
    target_name = _blank_to_none(as_username)
    if target_name is None:
        row = _get_user_by_id(conn, user["id"])
        if not row:
            raise HTTPException(status_code=404, detail="账号不存在")
        return row
    row = _get_user(conn, target_name)
    if not row or row["kind"] != "agent" or row["owner_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="只能管理自己名下的 Agent")
    return row


def _profile_fields(row) -> dict:
    """把账号的 2D/3D 形象字段整理成响应片段。row 需含 username 与形象列。"""
    username = row["username"]
    external = _row_get(row, "model3d_url")
    has_file = _row_get(row, "has_model3d")
    if has_file is None:
        has_file = _row_get(row, "model3d") is not None
    # 资源库引用改写成按引用授权的代理地址：任何客户端拿到后带自己的
    # token 就能下载（权限在下载接口按「同房间成员/资源主」判定），渲染端
    # 不需要认识 library: 前缀。
    if external and _library_ref_id(external) is not None:
        model_url = _model3d_file_url(username, _row_get(row, "model3d_updated_at"))
    elif external:
        model_url = external
    elif has_file:
        model_url = _model3d_file_url(username, _row_get(row, "model3d_updated_at"))
    else:
        model_url = None
    return {
        "avatarUrl": _avatar_url(username, _row_get(row, "avatar_updated_at")),
        "model3dUrl": model_url,
        "model3dArkit": bool(_row_get(row, "model3d_arkit", 0)),
        "model3dHumanoid": bool(_row_get(row, "model3d_humanoid", 0)),
    }


# 形象相关的轻量列：不含 avatar/model3d 的 BLOB 本体，避免列表响应被大字段撑爆
PROFILE_COLUMNS = """
    username, kind, status, created_at,
    avatar_updated_at, model3d_url, model3d_arkit, model3d_humanoid, model3d_updated_at,
    (model3d IS NOT NULL) AS has_model3d
"""



def _get_user(conn, username: str):
    return conn.execute(
        """
        SELECT id, username, kind, owner_id, public_key, status, created_at,
               avatar_updated_at, model3d_url, model3d_arkit, model3d_humanoid, model3d_updated_at,
               (model3d IS NOT NULL) AS has_model3d
        FROM users WHERE username = ?
        """,
        (username,),
    ).fetchone()


def _user_by_id(conn, user_id: int):
    return conn.execute(
        "SELECT id, username, kind, owner_id, status, avatar_updated_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()


ROOM_SELECT = """
        SELECT r.id, r.name, r.created_by, r.password_hash, r.visibility, r.muted,
               r.ended_at, r.archived_at, r.created_at, r.rules, r.room_agent_id, r.template,
               r.map3d, r.scene_updated_at, r.files_locked, r.files_revision,
               r.namespace_owner_id,
               r.room_type, r.pair_user_a_id, r.pair_user_b_id,
               u.username AS ownerName, u.kind AS creatorKind, u.owner_id AS creatorOwnerId,
               u.username AS creatorName, no.username AS namespaceOwnerName,
               ra.username AS roomAgentName
        FROM rooms r
        JOIN users u ON u.id = r.created_by
        LEFT JOIN users no ON no.id = r.namespace_owner_id
        LEFT JOIN users ra ON ra.id = r.room_agent_id
"""


def _room_reference_error() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "room_reference_required",
            "message": "同名私聊房间不止一个，请使用 roomId 或 roomKey",
        },
    )


def _active_rooms_by_name(conn, room_name: str):
    return conn.execute(
        ROOM_SELECT + " WHERE r.name = ? COLLATE NOCASE AND r.archived_at IS NULL",
        (room_name,),
    ).fetchall()


def _get_room(conn, room_name: str, user_id: int | None = None):
    # 让既有 `/api/rooms/{room_name}/...` 路由也能渐进接受稳定 roomId/roomKey，
    # 不必一次性复制几十个端点；新客户端优先传 roomId 或完整 roomKey。
    if str(room_name).isdigit():
        return _get_room_by_id(conn, int(room_name))
    if ":" in str(room_name):
        return _get_room_by_key(conn, str(room_name))
    rows = _active_rooms_by_name(conn, room_name)
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]
    if user_id is not None:
        candidates = [
            row for row in rows
            if row["namespace_owner_id"] == user_id
            or row["created_by"] == user_id
            or conn.execute(
                "SELECT 1 FROM room_members WHERE room_id = ? AND user_id = ?",
                (row["id"], user_id),
            ).fetchone()
        ]
        if len(candidates) == 1:
            return candidates[0]
    raise _room_reference_error()


def _get_room_by_key(conn, room_key: str):
    if ":" not in room_key:
        return None
    owner_name, room_name = room_key.split(":", 1)
    owner_name = owner_name.strip()
    room_name = room_name.strip()
    if not owner_name or not room_name:
        return None
    return conn.execute(
        ROOM_SELECT
        + " WHERE no.username = ? COLLATE NOCASE AND r.name = ? COLLATE NOCASE"
        + " AND r.archived_at IS NULL",
        (owner_name, room_name),
    ).fetchone()


def _resolve_room_reference(
    conn,
    *,
    room_id: int | None = None,
    room_key: str | None = None,
    room_name: str | None = None,
    user_id: int | None = None,
):
    if room_id is not None:
        return _get_room_by_id(conn, room_id)
    if room_key:
        return _get_room_by_key(conn, room_key)
    if room_name:
        return _get_room(conn, room_name, user_id=user_id)
    return None


def _room_key(room) -> str:
    namespace = room["namespaceOwnerName"] or room["ownerName"]
    return f"{namespace}:{room['name']}"


def _get_room_by_id(conn, room_id: int):
    return conn.execute(ROOM_SELECT + " WHERE r.id = ?", (room_id,)).fetchone()


def _is_ended(room) -> bool:
    return bool(room["ended_at"] or room["archived_at"])


def _touch(conn, room_id: int, user_id: int) -> None:
    conn.execute(
        """
        UPDATE room_members SET last_seen_at = datetime('now')
        WHERE room_id = ? AND user_id = ?
        """,
        (room_id, user_id),
    )


def _mark_room_read(conn, room_id: int, user_id: int) -> None:
    conn.execute(
        """
        UPDATE room_members
        SET last_read_msg_id = MAX(
            last_read_msg_id,
            (SELECT COALESCE(MAX(id), 0) FROM messages WHERE room_id = ?)
        )
        WHERE room_id = ? AND user_id = ?
        """,
        (room_id, room_id, user_id),
    )


def _member_any(conn, room_id: int, user_id: int):
    return conn.execute(
        "SELECT * FROM room_members WHERE room_id = ? AND user_id = ?",
        (room_id, user_id),
    ).fetchone()


def _member(conn, room_id: int, user_id: int):
    return conn.execute(
        """
        SELECT * FROM room_members
        WHERE room_id = ? AND user_id = ? AND membership_status = 'active'
        """,
        (room_id, user_id),
    ).fetchone()


def _active_room_members(conn, room) -> list[dict]:
    rows = conn.execute(
        """
        SELECT m.*, u.username, u.kind, u.avatar_updated_at AS avatarV,
               CASE WHEN m.last_seen_at > datetime('now', ?)
                    AND NOT EXISTS (
                        SELECT 1 FROM room_bans b WHERE b.room_id = m.room_id AND b.user_id = m.user_id
                          AND (b.expires_at IS NULL OR b.expires_at > strftime('%Y-%m-%d %H:%M:%f', 'now'))
                    ) THEN 1 ELSE 0 END AS online
        FROM room_members m JOIN users u ON u.id = m.user_id
        WHERE m.room_id = ? AND m.membership_status = 'active'
        ORDER BY m.joined_at ASC, m.user_id ASC
        """,
        (ONLINE_WINDOW, room["id"]),
    ).fetchall()
    return [
        {
            "userId": row["user_id"],
            "username": row["username"],
            "kind": row["kind"],
            "avatarUrl": _avatar_url(row["username"], row["avatarV"]),
            "joinedAt": row["joined_at"],
            "online": bool(row["online"]),
            "inRoom": bool(row["online"]),
            "canSpeak": _is_room_governor(room, row["user_id"])
            or (not room["muted"] and bool(row["can_speak"])),
            "canUpload": bool(row["can_upload"]),
            "canViewHistory": bool(row["can_view_history"]),
            "canEditFiles": bool(row["can_edit_files"]),
            "isRoomOwner": row["user_id"] == room["created_by"],
        }
        for row in rows
    ]


def _online_users(conn, room) -> list[dict]:
    """在线成员（带有效发言权 canSpeak，供前端画红/绿框）。

    有效发言权 = 房主/roomAgent 恒可发言；否则受全体禁言与成员禁言影响。
    另附 3D 渲染端所需的形象字段（model3dUrl/Arkit/Humanoid，轻量列不含 BLOB 本体）
    与 isRoomOwner 标识——2D 端忽略这些多余键，属纯增量。
    """
    gov_ids = {room["created_by"]}
    if room["room_agent_id"]:
        gov_ids.add(room["room_agent_id"])
    rows = conn.execute(
        """
        SELECT u.username, u.kind, u.avatar_updated_at AS avatarV, m.last_seen_at AS lastSeenAt,
               u.id AS uid, m.can_speak AS canSpeak,
               u.model3d_url, u.model3d_arkit, u.model3d_humanoid, u.model3d_updated_at,
               (u.model3d IS NOT NULL) AS has_model3d
        FROM room_members m
        JOIN users u ON u.id = m.user_id
        WHERE m.room_id = ? AND m.membership_status = 'active'
          AND m.last_seen_at > datetime('now', ?)
          -- 被封禁者立即从在线列表消失，不等 last_seen_at 自然过期
          AND NOT EXISTS (
              SELECT 1 FROM room_bans b
              WHERE b.room_id = m.room_id AND b.user_id = m.user_id
                AND (b.expires_at IS NULL OR b.expires_at > strftime('%Y-%m-%d %H:%M:%f', 'now'))
          )
        ORDER BY m.last_seen_at DESC
        """,
        (room["id"], ONLINE_WINDOW),
    ).fetchall()
    return [
        {
            "username": row["username"],
            # XR 端需要区分人类与 Agent 的朝向约定：历史上 Agent 已自行补过 180°，
            # 人类客户端则由渲染端统一修正。这个字段不影响 2D 展示。
            "kind": row["kind"],
            # 3D 位姿的二进制帧用数字 id 指代成员，客户端据此把 id 映射回用户名（2D 忽略此键）
            "userId": row["uid"],
            "lastSeenAt": row["lastSeenAt"],
            **_profile_fields(row),
            "canSpeak": row["uid"] in gov_ids
            or (not room["muted"] and bool(row["canSpeak"])),
            "isRoomOwner": row["uid"] == room["created_by"],
        }
        for row in rows
    ]


def _require_active_room(conn, room_name: str, user_id: int | None = None):
    room = _get_room(conn, room_name, user_id=user_id)
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在，请先创建或加入")
    if _is_ended(room):
        raise HTTPException(status_code=410, detail="房间已结束")
    return room


def _reject_if_banned(conn, room, user_id: int) -> None:
    """有效封禁（未到期或永久）期间：无法加入房间、无法读取任何房间数据。
    过期行保留在封禁名单里作记录，但不再拦截。放在 _require_membership 与
    入房流程的最前面，优先于「尚未加入」「需要密码」等提示。"""
    row = conn.execute(
        """
        SELECT expires_at FROM room_bans
        WHERE room_id = ? AND user_id = ?
        """,
        (room["id"], user_id),
    ).fetchone()
    if not row:
        return
    if row["expires_at"] is not None and row["expires_at"] <= _db_now(conn):
        return
    if row["expires_at"] is None:
        raise HTTPException(status_code=403, detail="你已被本房间封禁（永久）")
    raise HTTPException(status_code=403, detail=f"你已被本房间封禁，至 {row['expires_at']}（UTC）")


def _require_membership(conn, room_name: str, user_id: int):
    room = _require_active_room(conn, room_name, user_id=user_id)
    _reject_if_banned(conn, room, user_id)
    member = _member(conn, room["id"], user_id)
    if not member:
        raise HTTPException(status_code=403, detail="尚未加入该房间")
    _touch(conn, room["id"], user_id)
    return room, member


def _is_room_governor(room, user_id: int) -> bool:
    """房间治理者：房主、roomAgent，或创建该房的 Agent 的人类主人（v2.28）。

    治理者可代房主执行管理 API、并可见全部私聊与完整历史。主人与房主同级：
    房是 Agent 替主人建的，主人理当能管。用 id 比对就够——`creatorOwnerId`
    取自创建者（agent）的 `owner_id`，而 agent 只能由人类创建、owner_id 恒指
    向那个人类；再要求 `creatorKind == 'agent'`，人类自建的房不会被误判。
    """
    return (
        room["created_by"] == user_id
        or room["room_agent_id"] == user_id
        or (
            _row_get(room, "creatorKind") == "agent"
            and _row_get(room, "creatorOwnerId") == user_id
        )
    )


def _require_owner(room, user_id: int) -> None:
    if not _is_room_governor(room, user_id):
        raise HTTPException(
            status_code=403,
            detail="只有房主、房间管理 Agent（roomAgent）或建房的 Agent 主人可以管理该房间",
        )


def _is_agent_master(user: dict, room) -> bool:
    """当前用户是否是创建该房间的 Agent 的主人（用于归档权：roomAgent 不可归档）。"""
    return (
        user.get("kind") == "human"
        and room["creatorKind"] == "agent"
        and room["creatorOwnerId"] == user["id"]
    )


def _can_archive(user: dict, room) -> bool:
    return room["created_by"] == user["id"] or _is_agent_master(user, room)


def _resolve_room_agent(conn, caller: dict, name: str | None) -> int | None:
    """把 room agent 用户名解析成 user id。

    只允许「房主自己名下的 Agent」或「房主本身就是该 Agent」，避免把房间治理权
    交给别人的 Agent。空值表示不设 room agent。
    """
    target_name = _blank_to_none(name)
    if target_name is None:
        return None
    row = _get_user(conn, target_name)
    if not row or row["kind"] != "agent":
        raise HTTPException(status_code=400, detail=f"{target_name} 不是 Agent 账号")
    if row["id"] != caller["id"] and row["owner_id"] != caller["id"]:
        raise HTTPException(status_code=400, detail=f"{target_name} 不是你名下的 Agent")
    return row["id"]


def _require_archive_access(conn, room_id: int, user: dict):
    room = _get_room_by_id(conn, room_id)
    if not room or not room["archived_at"]:
        raise HTTPException(status_code=404, detail="归档不存在")
    member = _member_any(conn, room["id"], user["id"])
    if member and member["membership_status"] == "removed":
        member = None
    if not _can_archive(user, room) and not member:
        raise HTTPException(status_code=403, detail="无权查看该归档")
    return room, member


def _check_action_allowed(room, member, user_id: int, action: str, whisper: bool = False) -> None:
    if _is_room_governor(room, user_id):
        return
    if room["muted"]:
        raise HTTPException(status_code=403, detail="房间已全体禁言")
    if whisper:
        # 私聊不受成员「公开发言」禁言限制（禁言只约束公开频道）；私聊可见性与
        # 可达性由 whisper-rules 白黑名单与目标校验管控（狼人杀等主持场景依赖此通道）。
        return
    if action == "speak" and not member["can_speak"]:
        raise HTTPException(status_code=403, detail="你已被禁言")
    if action == "upload" and not member["can_upload"]:
        raise HTTPException(status_code=403, detail="你已被禁止上传附件")


def _whisper_targets(conn, room, content: str, sender_id: int | None = None) -> list:
    """解析消息开头的私聊前缀，返回目标用户行列表（去重保序）；无前缀返回 []。

    两种前缀可混用（v2.8 起支持群组）：
    - @@用户名：发给指定房间成员；
    - #群组名：展开为该群组内除发送者外的全部房间成员——群组由房主/roomAgent
      维护，成员名单对非成员保密（游戏身份群如狼人群依赖这一点）。
    只能发言给自己所在的群（防伪装身份），治理者代发除外。
    目标必须可送达，否则报错——避免本想私聊的消息被当成公开消息广播出去。
    """
    room_id = room["id"]
    targets: list = []
    seen: set[str] = set()
    group_names: list[str] = []
    rest = content or ""
    while True:
        m = GROUP_RE.match(rest)
        if m:
            name = m.group(1)
            rest = rest[m.end():]
            if name.lower() not in {g.lower() for g in group_names}:
                group_names.append(name)
            continue
        m = WHISPER_RE.match(rest)
        if not m:
            break
        name = m.group(1)
        rest = rest[m.end():]
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        target = _get_user(conn, name)
        if not target:
            raise HTTPException(status_code=400, detail=f"私聊对象 {name} 不存在，请检查 @@用户名 是否正确")
        if not _member(conn, room_id, target["id"]):
            raise HTTPException(status_code=400, detail=f"私聊对象 {name} 不在该房间中")
        targets.append(target)
    for gname in group_names:
        group = _get_group(conn, room_id, gname)
        if not group:
            raise HTTPException(status_code=400, detail=f"群组 #{gname} 不存在，请让房主或管理 Agent 先创建")
        members = _group_members(conn, group["id"])
        if sender_id is not None and sender_id not in {m["id"] for m in members} \
                and not _is_room_governor(room, sender_id):
            raise HTTPException(status_code=403, detail=f"你不是群组 #{gname} 的成员，不能在群里发言")
        for target in members:
            if sender_id is not None and target["id"] == sender_id:
                continue
            if target["username"].lower() in seen:
                continue
            if not _member(conn, room_id, target["id"]):
                continue    # 已退出房间的旧成员自动跳过，不影响其余人收信
            seen.add(target["username"].lower())
            targets.append(target)
    if group_names and not targets:
        raise HTTPException(status_code=400, detail=f"群组 #{group_names[0]} 没有可发送的成员（成员需在房间内且不是你自己）")
    return targets


def _mention_target(conn, room, content: str):
    """解析开头单一 @TA 标记。direct pair 天然双人可见，不附加 mention 语义。"""
    if _row_get(room, "room_type", "standard") == "direct_pair":
        return None
    rest = content or ""
    while True:
        prefix = GROUP_RE.match(rest) or WHISPER_RE.match(rest)
        if not prefix:
            break
        rest = rest[prefix.end():]
    match = MENTION_RE.match(rest)
    if not match:
        return None
    name = match.group(1)
    target = _get_user(conn, name)
    if not target or not _member(conn, room["id"], target["id"]):
        raise _friend_error(
            "mention_target_not_member",
            f"@{name} 不是当前房间的活动成员",
            400,
        )
    return target


def _check_whisper_targets(conn, room_id: int, sender_name: str, targets: list) -> None:
    for target in targets:
        _require_whisper_allowed(conn, room_id, sender_name, target["username"])


def _whisper_ids(row) -> set[int]:
    """行内私聊接收者全集：whisper_to（旧行/单接收者）+ whisper_to_ids（v2.5 多人列表）。"""
    ids: set[int] = set()
    single = _row_get(row, "whisper_to")
    if single:
        ids.add(int(single))
    for part in str(_row_get(row, "whisper_to_ids") or "").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def _whisper_ordered_ids(row) -> list[int]:
    """接收者 id（保序去重）：whisper_to 打头，其后是 whisper_to_ids 列表。"""
    ordered: list[int] = []
    single = _row_get(row, "whisper_to")
    if single:
        ordered.append(int(single))
    for part in str(_row_get(row, "whisper_to_ids") or "").split(","):
        part = part.strip()
        if part.isdigit() and int(part) not in ordered:
            ordered.append(int(part))
    return ordered


def _whisper_users_map(conn, rows) -> dict[int, dict]:
    """批量取私聊接收者的用户名与头像；_message_dicts 用它组装 whisperTo。"""
    ids: set[int] = set()
    for row in rows:
        ids.update(_whisper_ordered_ids(row))
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    out: dict[int, dict] = {}
    for r in conn.execute(
        f"SELECT id, username, avatar_updated_at FROM users WHERE id IN ({placeholders})",
        tuple(ids),
    ):
        out[r["id"]] = {"username": r["username"], "avatarUrl": _avatar_url(r["username"], r["avatar_updated_at"])}
    return out


def _strip_whisper_prefix(content: str) -> str:
    """去掉开头的 @@用户名 前缀（仅用于渲染与引用摘要；存储与 API 内容保持原样）。"""
    text = content or ""
    while True:
        m = WHISPER_RE.match(text)
        if not m:
            return text
        text = text[m.end():]


def _whisper_columns(targets: list) -> tuple[int | None, str | None]:
    """由目标列表生成 (whisper_to, whisper_to_ids)：whisper_to 恒为第一个接收者。"""
    if not targets:
        return None, None
    ids = [t["id"] for t in targets]
    return ids[0], ",".join(str(i) for i in ids)


def _resolve_reply(conn, room, user: dict, reply_to: int | None) -> int | None:
    """校验引用目标并返回其 id；None 表示不引用。

    目标必须在本房间、未被撤回、且对发送者可见（看不见的私聊不能被引用，避免借引用泄露）。
    """
    if not reply_to:
        return None
    row = conn.execute(
        "SELECT id, room_id, user_id, whisper_to, whisper_to_ids, recalled FROM messages WHERE id = ?",
        (reply_to,),
    ).fetchone()
    if not row or row["room_id"] != room["id"]:
        raise HTTPException(status_code=404, detail="引用的消息不存在")
    if row["recalled"]:
        raise HTTPException(status_code=400, detail="引用的消息已撤回，不能引用")
    recipients = _whisper_ids(row)
    if recipients and user["id"] not in (row["user_id"], *recipients) and not _is_room_governor(room, user["id"]):
        raise HTTPException(status_code=403, detail="引用的消息对你不可见")
    return row["id"]


def _whisper_allowed(conn, room_id: int, sender_name: str, receiver_name: str) -> bool:
    """房间私聊规则判定（仅约束发送，不回溯历史消息）。

    所有匹配（sender/receiver 为 * 或与双方用户名 NOCASE 相等）的规则中，
    取优先级最高的一条生效；同优先级时黑名单（deny）优先；
    没有任何规则命中则默认允许——普通房间不配规则即等价于
    白名单「允许 * 发送给 *」。
    """
    rules = conn.execute(
        """
        SELECT list_type, priority FROM whisper_rules
        WHERE room_id = ?
          AND (sender = '*' OR sender = ? COLLATE NOCASE)
          AND (receiver = '*' OR receiver = ? COLLATE NOCASE)
        """,
        (room_id, sender_name, receiver_name),
    ).fetchall()
    if not rules:
        return True
    rules.sort(key=lambda r: (r["priority"], r["list_type"] == "deny"), reverse=True)
    return rules[0]["list_type"] == "allow"


def _require_whisper_allowed(conn, room_id: int, sender_name: str, receiver_name: str) -> None:
    if not _whisper_allowed(conn, room_id, sender_name, receiver_name):
        raise HTTPException(status_code=403, detail=f"房间的私聊规则不允许发给 {receiver_name}")


# ---------- 房间群组（v2.8：命名私聊群，如狼人杀的狼人群） ----------

def _get_group(conn, room_id: int, name: str):
    return conn.execute(
        "SELECT * FROM room_groups WHERE room_id = ? AND name = ?", (room_id, name)
    ).fetchone()


def _group_members(conn, group_id: int) -> list:
    return conn.execute(
        """
        SELECT u.* FROM room_group_members gm JOIN users u ON u.id = gm.user_id
        WHERE gm.group_id = ? ORDER BY u.id
        """,
        (group_id,),
    ).fetchall()


def _group_dicts(conn, room, user_id: int) -> list[dict]:
    """群组列表。可见性：治理者（房主/roomAgent）可见全部；普通成员只看见
    自己所在的群——群组常按游戏身份组建（如狼人群），名单对非成员保密。"""
    governor = _is_room_governor(room, user_id)
    out = []
    for row in conn.execute(
        "SELECT id, name FROM room_groups WHERE room_id = ? ORDER BY id", (room["id"],)
    ).fetchall():
        members = _group_members(conn, row["id"])
        if not governor and user_id not in {m["id"] for m in members}:
            continue
        out.append({"name": row["name"], "members": [m["username"] for m in members]})
    return out


def _validate_group_members(conn, room_id: int, names: list[str]) -> list[int]:
    """群组成员白名单：必须存在且在房间里；去重保序。"""
    ids: list[int] = []
    for name in names:
        u = _get_user(conn, name)
        if not u:
            raise HTTPException(status_code=400, detail=f"群组成员 {name} 不存在")
        if not _member(conn, room_id, u["id"]):
            raise HTTPException(status_code=400, detail=f"群组成员 {name} 不在该房间中")
        if u["id"] not in ids:
            ids.append(u["id"])
    return ids


def _room_dict(room, user_id: int, online: list[dict] | None = None) -> dict:
    data = {
        "roomId": room["id"],
        "roomKey": _room_key(room),
        "roomName": room["name"],
        "ownerName": room["ownerName"],
        "creatorName": room["creatorName"],
        "namespaceOwnerName": room["namespaceOwnerName"] or room["ownerName"],
        "visibility": room["visibility"],
        "roomType": _row_get(room, "room_type", "standard"),
        "isDirectPair": _row_get(room, "room_type", "standard") == "direct_pair",
        "hasPassword": bool(room["password_hash"]),
        "muted": bool(room["muted"]),
        "isOwner": room["created_by"] == user_id,
        "canArchive": room["created_by"] == user_id or room["creatorOwnerId"] == user_id,
        # 治理者（房主 / roomAgent / 建房的 Agent 主人）能开管理面板；归档仍单独看 canArchive
        "canManage": _is_room_governor(room, user_id),
        "rules": _fill_base_url(_row_get(room, "rules")),
        "roomAgent": _row_get(room, "roomAgentName"),
        "template": _row_get(room, "template"),
        "scene": _room_scene_descriptor(room),
        "createdAt": room["created_at"],
        "archivedAt": room["archived_at"],
    }
    if online is not None:
        data["onlineUsers"] = online
        data["onlineCount"] = len(online)
    return data


def _row_get(row, key, default=None):
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def _reply_dict(row, room, user_id: int | None) -> dict | None:
    """引用信息。原消息是私聊且请求者不可见时只给占位（hidden），不泄露内容。"""
    reply_id = _row_get(row, "reply_to")
    if not reply_id:
        return None
    reply_user_id = _row_get(row, "replyUserId")
    if reply_user_id is None:
        # 原消息行缺失（理论不可达：撤回走墓碑）
        return {"id": reply_id, "username": "", "excerpt": "", "excerptType": "text", "recalled": True, "hidden": False}
    username = _row_get(row, "replyUsername") or ""
    if _row_get(row, "replyRecalled"):
        return {"id": reply_id, "username": username, "excerpt": "", "excerptType": "text", "recalled": True, "hidden": False}
    original = {
        "user_id": reply_user_id,
        "whisper_to": _row_get(row, "replyWhisperTo"),
        "whisper_to_ids": _row_get(row, "replyWhisperIds"),
    }
    recipients = _whisper_ids(original)
    allowed = {int(reply_user_id), *recipients}
    if room is not None:
        if room["created_by"]:
            allowed.add(int(room["created_by"]))
        if room["room_agent_id"]:
            allowed.add(int(room["room_agent_id"]))
    if recipients and user_id is not None and user_id not in allowed:
        return {"id": reply_id, "username": username, "excerpt": "", "excerptType": "text", "recalled": False, "hidden": True}
    reply_type = _row_get(row, "replyType") or "text"
    if reply_type in ("attachment", "image", "voice"):
        excerpt = _row_get(row, "replyAttachment") or ""
        excerpt_type = reply_type
    else:
        excerpt = re.sub(r"\s+", " ", _strip_whisper_prefix(_row_get(row, "replyContent") or "")).strip()[:120]
        excerpt_type = "text"
    return {
        "id": reply_id,
        "username": username,
        "excerpt": excerpt,
        "excerptType": excerpt_type,
        "recalled": False,
        "hidden": False,
    }


def _message_dict(row, room_name: str, *, room=None, user_id: int | None = None, archive_id: int | None = None,
                  whisper_users: dict | None = None) -> dict:
    username = row["username"]
    item = {
        "id": row["id"],
        "username": username,
        "avatarUrl": _avatar_url(username, _row_get(row, "avatarV")) if username else None,
        "content": row["content"],
        "msgType": row["msg_type"],
        "createdAt": row["createdAt"],
        "streaming": bool(_row_get(row, "streaming", 0)),
        "whisper": bool(_row_get(row, "whisper_to") or _row_get(row, "whisper_to_ids")),
        "mentionTo": (
            {
                "username": _row_get(row, "mentionUsername"),
                "avatarUrl": _avatar_url(_row_get(row, "mentionUsername"), _row_get(row, "mentionAvatarV")),
            }
            if _row_get(row, "mention_to") and _row_get(row, "mentionUsername")
            else None
        ),
        "recalled": bool(_row_get(row, "recalled", 0)),
        "updatedAt": _row_get(row, "updatedAt") or row["createdAt"],
    }
    if item["whisper"] and whisper_users:
        recipients = [whisper_users[i] for i in _whisper_ordered_ids(row) if i in whisper_users]
        if recipients:
            item["whisperTo"] = recipients
    reply = _reply_dict(row, room, user_id)
    if reply is not None:
        item["reply"] = reply
    if row["msg_type"] in ("attachment", "image", "voice"):
        item["attachmentName"] = row["attachment_name"]
        if row["msg_type"] == "voice":
            item["durationMs"] = int(_row_get(row, "duration_ms", 0) or 0)
        if archive_id:
            item["downloadUrl"] = f"/api/archives/{archive_id}/attachments/{row['id']}"
        else:
            item["downloadUrl"] = f"/api/rooms/{room_name}/attachments/{row['id']}"
    return item


def _message_dicts(conn, rows, room_name: str, *, room=None, user_id: int | None = None,
                   archive_id: int | None = None) -> list[dict]:
    """批量组装消息响应（私聊接收者一次查库后统一注入 whisperTo，避免逐条 N+1）。"""
    whisper_users = _whisper_users_map(conn, rows)
    return [
        _message_dict(row, room_name, room=room, user_id=user_id, archive_id=archive_id, whisper_users=whisper_users)
        for row in rows
    ]


def _room_list_dict(row, user_id: int) -> dict:
    return {
        "roomId": row["id"],
        "roomKey": f"{row['namespaceOwnerName'] or row['ownerName']}:{row['name']}",
        "roomName": row["name"],
        "ownerName": row["ownerName"],
        "creatorName": row["creatorName"],
        "namespaceOwnerName": row["namespaceOwnerName"] or row["ownerName"],
        "visibility": row["visibility"],
        "roomType": _row_get(row, "room_type", "standard"),
        "isDirectPair": _row_get(row, "room_type", "standard") == "direct_pair",
        "hasPassword": bool(row["password_hash"]),
        "muted": bool(row["muted"]),
        "isOwner": row["created_by"] == user_id,
        "canArchive": row["created_by"] == user_id or row["creatorOwnerId"] == user_id,
        "joined": bool(row["joined"]),
        "memberCount": row["memberCount"],
        "onlineCount": int(row["onlineCount"] or 0),
        "roomAgent": _row_get(row, "roomAgentName"),
        "scene": _room_scene_descriptor(row),
        "createdAt": row["created_at"],
        "archivedAt": row["archived_at"],
        "createdByMyAgent": (
            row["created_by"] != user_id and row["creatorOwnerId"] == user_id
        ),
        "unreadCount": int(_row_get(row, "unreadCount", 0) or 0),
    }


ROOM_LIST_SQL = """
    SELECT r.id, r.name, r.created_by, r.password_hash, r.visibility, r.muted,
           r.created_at, r.archived_at, r.map3d, r.scene_updated_at,
           r.namespace_owner_id,
           r.room_type, r.pair_user_a_id, r.pair_user_b_id,
           u.username AS ownerName, u.username AS creatorName,
           no.username AS namespaceOwnerName, u.owner_id AS creatorOwnerId,
           ra.username AS roomAgentName,
           (SELECT COUNT(*) FROM room_members m2
             WHERE m2.room_id = r.id AND m2.membership_status = 'active') AS memberCount,
           (SELECT COUNT(*) FROM room_members m3
             WHERE m3.room_id = r.id AND m3.membership_status = 'active'
               AND m3.last_seen_at > datetime('now', ?)) AS onlineCount,
           CASE WHEN m.user_id IS NULL THEN 0 ELSE 1 END AS joined,
           COALESCE((
             SELECT COUNT(*) FROM messages msg
             WHERE m.user_id IS NOT NULL
               AND msg.room_id = r.id
               AND msg.user_id != m.user_id
               AND msg.id > COALESCE(m.last_read_msg_id, 0)
               AND (m.can_view_history = 1 OR msg.id > COALESCE(m.first_visible_msg_id, 0))
               AND COALESCE(msg.recalled, 0) = 0
               AND (
                 COALESCE(msg.whisper_to, '') = '' AND COALESCE(msg.whisper_to_ids, '') = ''
                 OR msg.whisper_to = m.user_id
                 OR (',' || COALESCE(msg.whisper_to_ids, '') || ',') LIKE '%,' || m.user_id || ',%'
                 OR r.created_by = m.user_id
               )
           ), 0) AS unreadCount
    FROM rooms r
    JOIN users u ON u.id = r.created_by
    LEFT JOIN users no ON no.id = r.namespace_owner_id
    LEFT JOIN users ra ON ra.id = r.room_agent_id
    LEFT JOIN room_members m ON m.room_id = r.id AND m.user_id = ? AND m.membership_status = 'active'
"""


@app.get("/api/health")
def health():
    return {"ok": True}


# ---------- 人类账户（v2.25：手机短信 / 邮箱验证码） ----------

def _login_payload(user) -> dict:
    return {
        "token": auth.create_token(
            user["id"], user["username"], user["kind"], user["token_epoch"] or 0
        ),
        "username": user["username"],
        "userId": user["id"],
        "kind": user["kind"],
    }


def _get_user_by_contact(conn, channel: str, target: str):
    """按手机或邮箱找账号。标识符解析顺序固定为「手机 → 邮箱」，永不解析用户名
    （用户名允许纯数字，可能和手机号撞车）。"""
    column = "phone" if channel == "phone" else "email"
    return conn.execute(
        f"SELECT id, username, kind, status, password_hash, token_epoch"
        f" FROM users WHERE {column} = ?",
        (target,),
    ).fetchone()


def _contact_conflict(conn, channel: str, target: str, exclude_user_id: int | None = None) -> str | None:
    """手机/邮箱是否已被占用；返回冲突说明，可用则返回 None。"""
    column = "phone" if channel == "phone" else "email"
    sql = f"SELECT 1 FROM users WHERE {column} = ?"
    params: list[Any] = [target]
    if exclude_user_id is not None:
        sql += " AND id != ?"
        params.append(exclude_user_id)
    if conn.execute(sql, params).fetchone():
        return "该手机号已被使用" if channel == "phone" else "该邮箱已被使用"
    # 手机号还得排除与用户名撞车：否则「手机 → 邮箱」的解析顺序会让账号归属含糊
    if channel == "phone":
        sql = "SELECT 1 FROM users WHERE username = ?"
        params = [target]
        if exclude_user_id is not None:
            sql += " AND id != ?"
            params.append(exclude_user_id)
        if conn.execute(sql, params).fetchone():
            return "该号码与已有用户名重复，请换一个"
    return None


def _contact_fields(conn, user_id: int) -> dict:
    """本人联系方式的掩码与绑定状态。只在 /api/me 与绑定接口里返回——
    绝不能进 PROFILE_COLUMNS/_get_user，那会经在线成员、房间成员列表泄露给所有人。"""
    row = conn.execute(
        "SELECT phone, phone_verified_at, email, email_verified_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if not row:
        return {"phone": None, "phoneVerified": False, "email": None, "emailVerified": False}
    return {
        "phone": config.mask_target("phone", row["phone"]),
        "phoneVerified": bool(row["phone"] and row["phone_verified_at"]),
        "email": config.mask_target("email", row["email"]),
        "emailVerified": bool(row["email"] and row["email_verified_at"]),
    }


def _split_identifier(raw: str) -> tuple[str, str]:
    """把「手机号或邮箱」拆成 (channel, target)；顺序固定 手机 → 邮箱。"""
    text = (raw or "").strip()
    phone = config.normalize_phone(text)
    if phone:
        return "phone", phone
    email = config.normalize_email(text)
    if email:
        return "email", email
    raise HTTPException(status_code=400, detail="请填写正确的手机号或邮箱")


@app.get("/api/auth/channels")
def auth_channels():
    """公开：前端据此隐藏不可用入口，e2e 据此决定跑哪条分支。"""
    return {
        "phone": config.sms_available(),
        "email": config.email_available(),
        "required": config.verify_required(),
        # 仅调试模式（设了 WEBHARNESS_SMS_DEBUG_CODE）才回显，方便本地浏览器实测
        "debugCode": config.sms_debug_code(),
    }


@app.post("/api/auth/send-code")
def send_code(body: SendCodeRequest, request: Request):
    client = request.client.host if request.client else "unknown"
    wait = ratelimit.hit("send-code", client, config.send_code_per_minute(), 60.0)
    if wait > 0:
        raise HTTPException(status_code=429, detail=f"操作太频繁，请 {int(wait) + 1} 秒后再试")
    return verify_codes.send(body.channel, body.target, body.purpose)


@app.post("/api/users")
def create_user(body: UserCreate):
    avatar_bytes = None
    avatar_mime = None
    if body.avatar:
        data, declared = _decode_data_url(body.avatar)
        avatar_mime = _validate_avatar_bytes(data, declared)
        avatar_bytes = data

    phone = config.normalize_phone(body.phone) if body.phone else None
    email = config.normalize_email(body.email) if body.email else None
    if body.phone and not phone:
        raise HTTPException(status_code=400, detail="手机号格式不正确")
    if body.email and not email:
        raise HTTPException(status_code=400, detail="邮箱格式不正确")
    if config.verify_required() and not (phone or email):
        raise HTTPException(status_code=400, detail="请填写手机号或邮箱，并完成验证码验证")

    # 先查占用，避免白烧一个验证码；再把码核验掉（consume 自己开连接，故放在事务外）
    with get_db() as conn:
        if phone:
            conflict = _contact_conflict(conn, "phone", phone)
            if conflict:
                raise HTTPException(status_code=409, detail=conflict)
        if email:
            conflict = _contact_conflict(conn, "email", email)
            if conflict:
                raise HTTPException(status_code=409, detail=conflict)
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (body.username,)).fetchone():
            raise HTTPException(status_code=409, detail="用户名已存在")

    if phone:
        if not body.phoneCode:
            raise HTTPException(status_code=400, detail="请先获取手机验证码")
        verify_codes.consume("phone", phone, "register", body.phoneCode)
    if email:
        if not body.emailCode:
            raise HTTPException(status_code=400, detail="请先获取邮箱验证码")
        verify_codes.consume("email", email, "register", body.emailCode)

    with get_db() as conn:
        now = _db_now(conn)
        try:
            conn.execute(
                """
                INSERT INTO users (username, password_hash, kind, avatar, avatar_mime, avatar_updated_at,
                                   phone, phone_verified_at, email, email_verified_at)
                VALUES (?, ?, 'human', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    body.username,
                    auth.hash_password(body.password),
                    avatar_bytes,
                    avatar_mime,
                    now if avatar_bytes else None,
                    phone,
                    now if phone else None,
                    email,
                    now if email else None,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # 唯一索引兜底（上面的检查有竞态窗口）
            raise HTTPException(status_code=409, detail="用户名或联系方式已被占用") from exc
        user = _get_user(conn, body.username)
        _grant_points(
            conn,
            user_id=user["id"],
            amount=WELCOME_POINTS,
            transaction_type="signup_grant",
            idempotency_key=f"signup-welcome:{user['id']}",
            reason="New human account welcome grant",
            campaign_key=None,
        )
    return {
        "userId": user["id"],
        "username": user["username"],
        "createdAt": user["created_at"],
        "welcomePoints": WELCOME_POINTS,
        **_profile_fields(user),
    }


@app.post("/api/login")
def login(body: LoginRequest):
    if body.identifier and body.code:
        return _login_by_code(body.identifier, body.code)
    if body.username and body.password:
        with get_db() as conn:
            user = conn.execute(
                "SELECT id, username, kind, status, password_hash, token_epoch"
                " FROM users WHERE username = ?",
                (body.username,),
            ).fetchone()
        if (
            not user
            or user["kind"] != "human"
            or user["status"] != "active"
            or not auth.verify_password(body.password, user["password_hash"])
        ):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        return _login_payload(user)
    raise HTTPException(status_code=400, detail="请填写用户名密码，或用手机/邮箱验证码登录")


def _login_by_code(identifier: str, code: str) -> dict:
    if not config.any_channel_available():
        raise HTTPException(status_code=400, detail="验证码登录未启用")
    channel, target = _split_identifier(identifier)
    verified = verify_codes.consume(channel, target, "login", code)
    with get_db() as conn:
        user = _get_user_by_contact(conn, channel, verified)
    if not user or user["kind"] != "human" or user["status"] != "active":
        raise HTTPException(
            status_code=401, detail="该手机号或邮箱尚未绑定账号，请先用用户名密码登录后绑定"
        )
    return _login_payload(user)


@app.post("/api/auth/reset-password")
def reset_password(body: PasswordReset):
    """忘记密码：手机/邮箱验证码 + 新密码。成功后所有旧会话立即失效。"""
    if not config.any_channel_available():
        raise HTTPException(status_code=400, detail="密码重置未启用")
    channel, target = _split_identifier(body.identifier)
    with get_db() as conn:
        user = _get_user_by_contact(conn, channel, target)
    if not user or user["kind"] != "human" or user["status"] != "active":
        raise HTTPException(status_code=404, detail="该手机号或邮箱尚未绑定账号")
    verify_codes.consume(channel, target, "reset", body.code)
    _set_password(user["id"], body.newPassword, user["password_hash"])
    return {"ok": True, "username": user["username"]}


def _set_password(user_id: int, new_password: str, current_hash: str) -> None:
    """改密 + token_epoch +1（踢掉所有旧会话）。"""
    if auth.verify_password(new_password, current_hash):
        raise HTTPException(status_code=400, detail="新密码不能与当前密码相同")
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, token_epoch = token_epoch + 1 WHERE id = ?",
            (auth.hash_password(new_password), user_id),
        )


@app.put("/api/me/password")
def change_password(body: PasswordChange, user: HumanUser):
    with get_db() as conn:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
        # 用 400 而不是 401：前端 api() 见到带 token 的 401 会直接登出，
        # 而这里只是「重新验证失败」，不该把用户踢下线
        if not row or not auth.verify_password(body.oldPassword, row["password_hash"]):
            raise HTTPException(status_code=400, detail="当前密码不正确")
        current_hash = row["password_hash"]
    _set_password(user["id"], body.newPassword, current_hash)
    # 改密会连本机一起踢下线，所以顺手补发一个新 token，前端换上后无需重新登录
    with get_db() as conn:
        epoch = conn.execute(
            "SELECT token_epoch FROM users WHERE id = ?", (user["id"],)
        ).fetchone()["token_epoch"]
    return {
        "ok": True,
        "token": auth.create_token(user["id"], user["username"], "human", epoch),
    }


@app.put("/api/me/contacts")
def bind_contact(body: ContactUpdate, user: HumanUser):
    """绑定 / 换绑手机或邮箱：新目标的验证码 + 当前密码（身份确认）。

    换绑用「当前密码」而不是「旧号验证码」：密码在注册时必填、恒可用，UI 也只要一个
    密码框，不依赖旧号还能收码。
    """
    normalized = config.normalize_target(body.channel, body.target)
    if not normalized:
        raise HTTPException(
            status_code=400,
            detail="手机号格式不正确" if body.channel == "phone" else "邮箱格式不正确",
        )
    with get_db() as conn:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
        if not row or not auth.verify_password(body.password, row["password_hash"]):
            raise HTTPException(status_code=400, detail="密码不正确")
        conflict = _contact_conflict(conn, body.channel, normalized, exclude_user_id=user["id"])
        if conflict:
            raise HTTPException(status_code=409, detail=conflict)
    verified = verify_codes.consume(body.channel, body.target, "bind", body.code)
    column = "phone" if body.channel == "phone" else "email"
    with get_db() as conn:
        now = _db_now(conn)
        try:
            conn.execute(
                f"UPDATE users SET {column} = ?, {column}_verified_at = ? WHERE id = ?",
                (verified, now, user["id"]),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="该手机号或邮箱已被占用") from exc
        contacts = _contact_fields(conn, user["id"])
    return {"ok": True, **contacts}


@app.post("/api/me/contacts/unbind")
def unbind_contact(body: ContactUnbind, user: HumanUser):
    """解绑手机或邮箱。用 POST 而非 DELETE：密码必须放请求体，不能进 URL（访问日志）。"""
    column = "phone" if body.channel == "phone" else "email"
    with get_db() as conn:
        row = conn.execute(
            f"SELECT password_hash, {column} AS target FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
        if not row or not auth.verify_password(body.password, row["password_hash"]):
            raise HTTPException(status_code=400, detail="密码不正确")
        if not row["target"]:
            raise HTTPException(
                status_code=400,
                detail="尚未绑定手机号" if body.channel == "phone" else "尚未绑定邮箱",
            )
        conn.execute(
            f"UPDATE users SET {column} = NULL, {column}_verified_at = NULL WHERE id = ?",
            (user["id"],),
        )
        contacts = _contact_fields(conn, user["id"])
    return {"ok": True, **contacts}


# ---------- Agent 登录（challenge-response） ----------

@app.post("/api/agent-auth/challenge")
def agent_challenge(body: ChallengeRequest):
    with get_db() as conn:
        agent = _get_user(conn, body.username)
        if not agent or agent["kind"] != "agent" or agent["status"] != "active":
            raise HTTPException(status_code=401, detail="Agent 不存在或不可用")
        nonce = auth.new_nonce()
        conn.execute("DELETE FROM agent_challenges WHERE user_id = ?", (agent["id"],))
        conn.execute(
            """
            INSERT INTO agent_challenges (nonce, user_id, expires_at)
            VALUES (?, ?, datetime('now', ?))
            """,
            (nonce, agent["id"], f"+{auth.CHALLENGE_TTL_MINUTES} minutes"),
        )
        expires = conn.execute(
            "SELECT expires_at FROM agent_challenges WHERE nonce = ?", (nonce,)
        ).fetchone()["expires_at"]
    return {"nonce": nonce, "expiresAt": expires}


@app.post("/api/agent-auth/login")
def agent_login(body: AgentLoginRequest):
    with get_db() as conn:
        agent = _get_user(conn, body.username)
        if not agent or agent["kind"] != "agent" or agent["status"] != "active":
            raise HTTPException(status_code=401, detail="Agent 不存在或不可用")
        challenge = conn.execute(
            """
            SELECT nonce, expires_at FROM agent_challenges
            WHERE user_id = ? AND expires_at > datetime('now')
            ORDER BY created_at DESC LIMIT 1
            """,
            (agent["id"],),
        ).fetchone()
        conn.execute("DELETE FROM agent_challenges WHERE user_id = ?", (agent["id"],))
        if not challenge:
            raise HTTPException(status_code=401, detail="challenge 不存在或已过期，请重新获取")
        if not auth.verify_agent_signature(agent["public_key"], challenge["nonce"], body.signature):
            raise HTTPException(status_code=401, detail="签名验证失败")
    return {
        "token": auth.create_token(agent["id"], agent["username"], "agent"),
        "username": agent["username"],
        "userId": agent["id"],
        "kind": "agent",
    }


# ---------- 当前用户 ----------

@app.get("/api/me")
def me(user: CurrentUser):
    result = {"id": user["id"], "username": user["username"], "kind": user["kind"],
              "isSuperadmin": user["isSuperadmin"],
              "adminCapabilities": list(ADMIN_CAPABILITIES) if user["isSuperadmin"] else []}
    with get_db() as conn:
        row = _get_user(conn, user["username"])
        if row:
            result.update(_profile_fields(row))
            if row["owner_id"]:
                owner = conn.execute("SELECT username FROM users WHERE id = ?", (row["owner_id"],)).fetchone()
                result["ownerName"] = owner["username"] if owner else None
            if user["kind"] == "human":
                # 联系方式只在自己这里回显（掩码），不进 PROFILE_COLUMNS
                result.update(_contact_fields(conn, row["id"]))
    return result


# ---------- 建议反馈（人类与 Agent 均可提交，需登录） ----------

@app.post("/api/suggestions")
def create_suggestion(body: SuggestionCreate, user: CurrentUser):
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="建议内容不能为空")
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO suggestions (content, contact, kind, username, category) VALUES (?, ?, ?, ?, ?)",
            (content, _blank_to_none(body.contact), user["kind"], user["username"], body.category),
        )
    return {"ok": True, "id": cur.lastrowid}


# ---------- 平台管理（超级管理员；与房间管理权限隔离） ----------

def _suggestion_dict(row) -> dict:
    return {
        "id": row["id"], "content": row["content"], "contact": row["contact"],
        "kind": row["kind"], "username": row["username"], "category": row["category"],
        "status": row["status"], "adminNote": row["admin_note"],
        "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        "updatedBy": row["updated_by_name"],
    }


SUGGESTION_SELECT = """
    SELECT s.*, u.username AS updated_by_name
    FROM suggestions s LEFT JOIN users u ON u.id = s.updated_by
"""


@app.get("/api/admin")
def admin_overview(user: SuperAdmin):
    """平台管理入口；后续管理模块继续复用 SuperAdmin 与能力列表。"""
    with get_db() as conn:
        counts = {row["status"]: row["n"] for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM suggestions GROUP BY status"
        )}
    return {
        "username": user["username"], "capabilities": list(ADMIN_CAPABILITIES),
        "suggestionCounts": {status: counts.get(status, 0) for status in SUGGESTION_STATUSES},
        "totalSuggestions": sum(counts.values()),
    }


@app.get("/api/admin/suggestions")
def list_suggestions(
    user: SuperAdmin,
    status: SuggestionStatus | None = None,
    category: SuggestionCategory | None = None,
    kind: Literal["human", "agent"] | None = None,
    beforeId: int | None = Query(default=None, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
):
    """按 id 倒序分页；total 为当前筛选总数，nextBeforeId 为下一页游标。"""
    clauses, params = [], []
    for column, value in (("status", status), ("category", category), ("kind", kind)):
        if value is not None:
            clauses.append(f"s.{column} = ?")
            params.append(value)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM suggestions s" + where, params).fetchone()[0]
        if beforeId is not None:
            clauses.append("s.id < ?")
            params.append(beforeId)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = conn.execute(SUGGESTION_SELECT + where + " ORDER BY s.id DESC LIMIT ?",
                            [*params, limit + 1]).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {"suggestions": [_suggestion_dict(row) for row in rows], "total": total,
            "nextBeforeId": rows[-1]["id"] if has_more else None}


@app.get("/api/admin/suggestions/{suggestion_id}")
def get_suggestion(suggestion_id: int, user: SuperAdmin):
    with get_db() as conn:
        row = conn.execute(SUGGESTION_SELECT + " WHERE s.id = ?", (suggestion_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="建议不存在")
    return _suggestion_dict(row)


@app.patch("/api/admin/suggestions/{suggestion_id}")
def update_suggestion(suggestion_id: int, body: SuggestionUpdate, user: SuperAdmin):
    fields, values = [], []
    if body.status is not None:
        fields.append("status = ?")
        values.append(body.status)
    if body.adminNote is not None:
        fields.append("admin_note = ?")
        values.append(body.adminNote.strip())
    if not fields:
        raise HTTPException(status_code=422, detail="请提供处理状态或管理员备注")
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE suggestions SET " + ", ".join(fields)
            + ", updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now'), updated_by = ? WHERE id = ?",
            [*values, user["id"], suggestion_id],
        )
        if not cur.rowcount:
            raise HTTPException(status_code=404, detail="建议不存在")
        row = conn.execute(SUGGESTION_SELECT + " WHERE s.id = ?", (suggestion_id,)).fetchone()
    return _suggestion_dict(row)


# ---------- Agent 账户管理（仅人类主人） ----------

def _agent_dict(row) -> dict:
    return {
        "username": row["username"],
        "status": row["status"],
        "createdAt": row["created_at"],
        **_profile_fields(row),
    }


@app.post("/api/agents")
def create_agent(body: AgentCreate, user: HumanUser):
    try:
        pem = auth.normalize_agent_public_key(body.publicKey)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    avatar_bytes = None
    avatar_mime = None
    if body.avatar:
        data, declared = _decode_data_url(body.avatar)
        avatar_mime = _validate_avatar_bytes(data, declared)
        avatar_bytes = data
    model_url = _blank_to_none(body.model3dUrl)
    # 内置形象引用同样要在写入前校验（未知 id 直接 400），能力位以目录为准
    builtin_id = _builtin_avatar_id(model_url)
    if builtin_id:
        meta = BUILTIN_AVATAR_BY_ID[builtin_id]
        model_arkit, model_humanoid = int(meta["arkit"]), int(meta["humanoid"])
    else:
        model_arkit, model_humanoid = int(body.model3dArkit), int(body.model3dHumanoid)
    with get_db() as conn:
        exists = _get_user(conn, body.username)
        if exists:
            if exists["kind"] == "agent":
                raise HTTPException(status_code=409, detail=f"用户名 {body.username} 已被其他 Agent 占用")
            raise HTTPException(
                status_code=409,
                detail=f"用户名 {body.username} 已被人类账号占用，请给 Agent 换一个名字（例如 {body.username}-bot）",
            )
        # 预检同名房间，避免已经写入 Agent 后才发现房间冲突；private 房间按主人
        # 的命名空间判定，public 房间仍是全局名称。
        if body.createRoom:
            existing_room = conn.execute(
                """
                SELECT 1 FROM rooms
                WHERE name = ? COLLATE NOCASE AND archived_at IS NULL
                  AND (
                    visibility = 'public'
                    OR (visibility = 'private' AND namespace_owner_id = ?)
                  )
                LIMIT 1
                """,
                (body.username, user["id"]),
            ).fetchone()
            if existing_room:
                raise HTTPException(status_code=409, detail=f"同名房间 {body.username} 已存在，请取消选项或更换 Agent 名称")
        now = _db_now(conn)
        conn.execute(
            """
            INSERT INTO users (
                username, password_hash, kind, owner_id, public_key,
                avatar, avatar_mime, avatar_updated_at,
                model3d_url, model3d_arkit, model3d_humanoid, model3d_updated_at
            )
            VALUES (?, '', 'agent', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                body.username,
                user["id"],
                pem,
                avatar_bytes,
                avatar_mime,
                now if avatar_bytes else None,
                model_url,
                model_arkit,
                model_humanoid,
                now if model_url else None,
            ),
        )
        agent = _get_user(conn, body.username)
        result = _agent_dict(agent)
        if body.createRoom:
            # 私有、无密码的新房间与普通「创建房间」默认一致；房主人类立即加入，
            # 新 Agent 只登记为 roomAgent，待其自行登录后加入。
            cur = conn.execute(
                """
                INSERT INTO rooms (name, created_by, namespace_owner_id, password_hash, visibility, room_agent_id)
                VALUES (?, ?, ?, NULL, 'private', ?)
                """,
                (body.username, user["id"], user["id"], agent["id"]),
            )
            room = _get_room_by_id(conn, cur.lastrowid)
            watermark = conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS w FROM messages WHERE room_id = ?",
                (room["id"],),
            ).fetchone()["w"]
            conn.execute(
                """
                INSERT INTO room_members (
                    room_id, user_id, last_seen_at, first_visible_msg_id, last_read_msg_id
                )
                VALUES (?, ?, datetime('now'), ?, ?)
                """,
                (room["id"], user["id"], watermark, watermark),
            )
            room_data = {**_room_dict(room, user["id"], _online_users(conn, room)), "created": True, "joined": True}
            result["room"] = room_data
    return result


@app.get("/api/agents")
def list_agents(user: HumanUser):
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT id, {PROFILE_COLUMNS}
            FROM users WHERE owner_id = ? AND kind = 'agent'
            ORDER BY created_at DESC
            """,
            (user["id"],),
        ).fetchall()
    return {"agents": [_agent_dict(row) for row in rows]}


def _get_my_agent(conn, owner_id: int, username: str):
    agent = _get_user(conn, username)
    if not agent or agent["kind"] != "agent" or agent["owner_id"] != owner_id:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    return agent


@app.patch("/api/agents/{username}")
def update_agent(username: str, body: AgentUpdate, user: HumanUser):
    with get_db() as conn:
        agent = _get_my_agent(conn, user["id"], username)
        new_name = body.username.strip() if body.username else agent["username"]
        if new_name.lower() != agent["username"].lower():
            exists = conn.execute("SELECT 1 FROM users WHERE username = ?", (new_name,)).fetchone()
            if exists:
                raise HTTPException(status_code=409, detail="用户名已存在")
        pem = agent["public_key"]
        if body.publicKey is not None:
            try:
                pem = auth.normalize_agent_public_key(body.publicKey)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        status = body.status or agent["status"]
        conn.execute(
            "UPDATE users SET username = ?, public_key = ?, status = ? WHERE id = ?",
            (new_name, pem, status, agent["id"]),
        )
        if new_name != agent["username"]:
            conn.execute("DELETE FROM agent_challenges WHERE user_id = ?", (agent["id"],))
            # 私聊规则按用户名匹配，改名要级联，否则规则静默失效
            conn.execute(
                "UPDATE whisper_rules SET sender = ? WHERE sender = ? COLLATE NOCASE",
                (new_name, agent["username"]),
            )
            conn.execute(
                "UPDATE whisper_rules SET receiver = ? WHERE receiver = ? COLLATE NOCASE",
                (new_name, agent["username"]),
            )
        agent = _get_user(conn, new_name)
    return _agent_dict(agent)


@app.delete("/api/agents/{username}")
def delete_agent(username: str, user: HumanUser):
    with get_db() as conn:
        agent = _get_my_agent(conn, user["id"], username)
        has_history = conn.execute(
            """
            SELECT 1 FROM messages
            WHERE user_id = ? OR whisper_to = ?
               OR (',' || COALESCE(whisper_to_ids, '') || ',') LIKE ?
            LIMIT 1
            """,
            (agent["id"], agent["id"], f"%,{agent['id']},%"),
        ).fetchone()
        owns_room = conn.execute(
            "SELECT 1 FROM rooms WHERE created_by = ? LIMIT 1", (agent["id"],)
        ).fetchone()
        if has_history or owns_room:
            raise HTTPException(status_code=409, detail="该 Agent 已有聊天记录或创建过房间，请改为停用")
        # 私聊规则按用户名匹配，Agent 删除后规则会悬空，一并清掉
        conn.execute(
            "DELETE FROM whisper_rules WHERE sender = ? COLLATE NOCASE OR receiver = ? COLLATE NOCASE",
            (agent["username"], agent["username"]),
        )
        try:
            conn.execute("DELETE FROM users WHERE id = ?", (agent["id"],))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="该 Agent 存在关联记录，请改为停用") from exc
    return {"deleted": True, "username": username}


# ---------- 头像 / 3D 形象 ----------
# 本人用不带 ?as= 的接口；主人替自己名下的 Agent 设置时加 ?as=<agent 用户名>。

@app.get("/api/users/{username}/avatar")
def get_avatar(username: str, user: CurrentUser):
    with get_db() as conn:
        row = conn.execute(
            "SELECT username, avatar, avatar_mime FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    headers = {"Cache-Control": "private, max-age=86400"}
    if row["avatar"]:
        return Response(row["avatar"], media_type=row["avatar_mime"] or "image/jpeg", headers=headers)
    return Response(_default_avatar_svg(row["username"]), media_type="image/svg+xml", headers=headers)


@app.post("/api/me/avatar")
async def set_my_avatar(
    user: CurrentUser,
    file: UploadFile = File(...),
    as_username: Annotated[str | None, Query(alias="as")] = None,
):
    data = await file.read()
    mime = _validate_avatar_bytes(data, file.content_type)
    with get_db() as conn:
        target = _resolve_profile_target(conn, user, as_username)
        conn.execute(
            "UPDATE users SET avatar = ?, avatar_mime = ?, avatar_updated_at = ? WHERE id = ?",
            (data, mime, _db_now(conn), target["id"]),
        )
        row = _get_user_by_id(conn, target["id"])
    return {"username": row["username"], **_profile_fields(row)}


@app.delete("/api/me/avatar")
def delete_my_avatar(user: CurrentUser, as_username: Annotated[str | None, Query(alias="as")] = None):
    with get_db() as conn:
        target = _resolve_profile_target(conn, user, as_username)
        conn.execute(
            "UPDATE users SET avatar = NULL, avatar_mime = NULL, avatar_updated_at = NULL WHERE id = ?",
            (target["id"],),
        )
        row = _get_user_by_id(conn, target["id"])
    return {"username": row["username"], **_profile_fields(row)}


@app.get("/api/users/{username}/model3d")
def get_model3d(username: str, user: CurrentUser):
    """下载账号的 3D 形象文件。来源有二（按 model3d_url 形态区分）：
    - `library:<id>` 资源库引用：请求者须是资源主本人（或名下 Agent），或与
      被引用账号至少同在一个未归档房间——按引用授权，资源被撤销/打回后即失效。
    - 旧版直传 BLOB（model3d 列）：保留到存量数据迁移完。
    找不到或无权一律 404（不泄露存在性）。"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, model3d, model3d_mime, model3d_url FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="该账号没有上传 3D 模型文件")
        library_id = _library_ref_id(_row_get(row, "model3d_url"))
        if library_id is not None:
            resource = _library_resource(conn, library_id)
            if not resource or resource["status"] != "active":
                raise HTTPException(status_code=404, detail="该账号没有上传 3D 模型文件")
            version = conn.execute(
                "SELECT * FROM library_resource_versions WHERE id = ?",
                (resource["current_version_id"],),
            ).fetchone()
            if not version or version["review_status"] != "approved" or version["scan_status"] != "clean":
                raise HTTPException(status_code=404, detail="该账号没有上传 3D 模型文件")
            if not _library_can_manage(resource, user) and not _shares_room(conn, row["id"], user):
                raise HTTPException(status_code=404, detail="该账号没有上传 3D 模型文件")
            resp = _library_version_file_response(version)
            resp.headers["Cache-Control"] = "private, max-age=86400"
            return resp
        if not row["model3d"]:
            raise HTTPException(status_code=404, detail="该账号没有上传 3D 模型文件")
        return Response(
            row["model3d"],
            media_type=row["model3d_mime"] or "model/gltf-binary",
            headers={"Cache-Control": "private, max-age=86400"},
        )


def _shares_room(conn, target_user_id: int, user: dict) -> bool:
    """请求者与目标账号是否同在一个未归档房间（形象代理下载的授权依据）。
    Agent 按主人折算（资源库命名空间同理），避免「主人看得见、Agent 看不见」。"""
    effective_id = _effective_human_id(user)
    if effective_id == target_user_id:
        return True
    row = conn.execute(
        """
        SELECT 1 FROM room_members m1
        JOIN room_members m2 ON m2.room_id = m1.room_id
        JOIN rooms r ON r.id = m1.room_id AND r.archived_at IS NULL
        LEFT JOIN users u1 ON u1.id = m1.user_id
        LEFT JOIN users u2 ON u2.id = m2.user_id
        WHERE COALESCE(u1.owner_id, m1.user_id) = ?
          AND COALESCE(u2.owner_id, m2.user_id) = ?
        LIMIT 1
        """,
        (target_user_id, effective_id),
    ).fetchone()
    return row is not None


@app.put("/api/me/model3d")
def set_my_model3d_url(
    body: Model3dUpdate,
    user: CurrentUser,
    as_username: Annotated[str | None, Query(alias="as")] = None,
):
    url = _blank_to_none(body.url)
    if url is None and body.arkit is None and body.humanoid is None:
        raise HTTPException(status_code=400, detail="没有需要修改的字段")
    # 内置形象引用（builtin:<id>）在这里就校验，未知 id 直接 400
    builtin_id = _builtin_avatar_id(url)
    library_id = _library_ref_id(url) if builtin_id is None else None
    with get_db() as conn:
        target = _resolve_profile_target(conn, user, as_username)
        # 资源库引用必须是本人/本 Agent 名下、已过审的模型资源
        if library_id is not None:
            _require_approved_library_model(conn, library_id, user, purpose="avatar")
        # 内置形象的能力位由目录决定（这些 VRM 确实有表情与骨骼），免得调用方漏填；
        # 显式传了 body.arkit/humanoid 时仍以调用方为准，外链与原有行为一字不变。
        if builtin_id:
            meta = BUILTIN_AVATAR_BY_ID[builtin_id]
            default_arkit = int(meta["arkit"])
            default_humanoid = int(meta["humanoid"])
        else:
            default_arkit = int(target["model3d_arkit"] or 0)
            default_humanoid = int(target["model3d_humanoid"] or 0)
        arkit = int(body.arkit) if body.arkit is not None else default_arkit
        humanoid = int(body.humanoid) if body.humanoid is not None else default_humanoid
        if url is None:
            conn.execute(
                "UPDATE users SET model3d_arkit = ?, model3d_humanoid = ? WHERE id = ?",
                (arkit, humanoid, target["id"]),
            )
        else:
            conn.execute(
                """
                UPDATE users SET model3d_url = ?, model3d_arkit = ?, model3d_humanoid = ?,
                                 model3d = NULL, model3d_mime = NULL, model3d_updated_at = ?
                WHERE id = ?
                """,
                (url, arkit, humanoid, _db_now(conn), target["id"]),
            )
        row = _get_user_by_id(conn, target["id"])
    return {"username": row["username"], **_profile_fields(row)}


@app.delete("/api/me/model3d")
def delete_my_model3d(user: CurrentUser, as_username: Annotated[str | None, Query(alias="as")] = None):
    with get_db() as conn:
        target = _resolve_profile_target(conn, user, as_username)
        conn.execute(
            """
            UPDATE users SET model3d = NULL, model3d_mime = NULL, model3d_url = NULL,
                             model3d_arkit = 0, model3d_humanoid = 0, model3d_updated_at = NULL
            WHERE id = ?
            """,
            (target["id"],),
        )
    return {"username": target["username"], "model3dUrl": None, "model3dArkit": False, "model3dHumanoid": False}


# ---------- 个人文件资源库（版本化、默认私有） ----------

LIBRARY_SELECT = """
    SELECT r.*, owner.username AS owner_name, owner.kind AS owner_kind,
           econ.username AS economic_owner_name,
           v.id AS current_version_id_joined, v.version_no AS current_version_no,
           v.name AS current_version_name, v.kind AS current_kind, v.mime AS current_mime,
           v.size AS current_size, v.sha256 AS current_sha256,
           v.scan_status AS current_scan_status, v.review_status AS current_review_status,
           v.created_at AS current_version_created_at,
           (SELECT rr.note FROM resource_reviews rr WHERE rr.resource_version_id = v.id ORDER BY rr.id DESC LIMIT 1) AS current_review_note,
           l.id AS current_listing_id, l.price_points AS current_listing_price, l.status AS current_listing_status
    FROM library_resources r
    JOIN users owner ON owner.id = r.owner_account_id
    JOIN users econ ON econ.id = r.economic_owner_id
    LEFT JOIN library_resource_versions v ON v.id = r.current_version_id
    LEFT JOIN resource_listings l ON l.resource_version_id = v.id
"""


def _library_resource(conn, resource_id: int):
    return conn.execute(LIBRARY_SELECT + " WHERE r.id = ?", (resource_id,)).fetchone()


def _library_version(conn, resource_id: int, version_id: int):
    return conn.execute(
        """
        SELECT v.*, r.owner_account_id, r.economic_owner_id, r.status AS resource_status,
               r.name AS resource_name, owner.username AS owner_name,
               econ.username AS economic_owner_name
        FROM library_resource_versions v
        JOIN library_resources r ON r.id = v.resource_id
        JOIN users owner ON owner.id = r.owner_account_id
        JOIN users econ ON econ.id = r.economic_owner_id
        WHERE v.resource_id = ? AND v.id = ?
        """,
        (resource_id, version_id),
    ).fetchone()


def _library_can_manage(resource, user: dict) -> bool:
    effective_id = _effective_human_id(user)
    return resource is not None and (
        resource["owner_account_id"] == effective_id
        or resource["economic_owner_id"] == effective_id
    )


def _require_approved_library_model(conn, resource_id: int, user: dict, *, purpose: str):
    """形象/房间场景引用资源库模型时的统一校验：资源必须存在、active、归当前
    用户（或其名下 Agent）管理，且当前版本已过审。purpose 仅用于错误提示。"""
    resource = _library_resource(conn, resource_id)
    if not resource or resource["status"] != "active" or not _library_can_manage(resource, user):
        raise HTTPException(status_code=404, detail="资源不存在")
    version = conn.execute(
        "SELECT * FROM library_resource_versions WHERE id = ?", (resource["current_version_id"],)
    ).fetchone()
    if not version or version["kind"] != "model":
        label = "3D 形象" if purpose == "avatar" else "房间场景"
        raise _friend_error("resource_kind_mismatch", f"{label}只能引用模型类资源", 409)
    if version["review_status"] != "approved" or version["scan_status"] != "clean":
        raise _friend_error("resource_not_approved", "资源尚未通过审核，请先在「我的资源库」提交审核", 409)
    return resource, version


def _library_version_file_response(version) -> FileResponse:
    """把资源库版本的内容文件作为下载响应（媒体类型取版本 mime）。"""
    path = LIBRARY_DIR / version["content_path"]
    if not version["content_path"] or not path.is_file():
        raise HTTPException(status_code=404, detail="资源文件缺失")
    return FileResponse(path, media_type=version["mime"] or "application/octet-stream")


def _library_resource_url(resource_id: int) -> str:
    return f"/api/library/resources/{resource_id}"


def _library_version_dict(row, *, include_content_url: bool = False) -> dict:
    data = {
        "id": row["id"],
        "resourceId": row["resource_id"],
        "versionNo": row["version_no"],
        "name": row["name"],
        "kind": row["kind"],
        "mime": row["mime"],
        "size": row["size"],
        "sha256": row["sha256"],
        "scanStatus": row["scan_status"],
        "reviewStatus": row["review_status"],
        "reviewNote": _row_get(row, "review_note", ""),
        "createdBy": row["created_by"],
        "createdAt": row["created_at"],
        "listing": (
            {
                "id": _row_get(row, "listing_id"),
                "pricePoints": _row_get(row, "listing_price", 0),
                "status": _row_get(row, "listing_status"),
            }
            if _row_get(row, "listing_id") is not None
            else None
        ),
    }
    if include_content_url:
        data["contentUrl"] = f"/api/library/resources/{row['resource_id']}/versions/{row['id']}/content"
    return data


def _library_resource_dict(conn, row, *, include_versions: bool = False) -> dict:
    data = {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "status": row["status"],
        "ownerAccountId": row["owner_account_id"],
        "ownerName": row["owner_name"],
        "economicOwnerName": row["economic_owner_name"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "currentVersion": None,
        "url": _library_resource_url(row["id"]),
    }
    if row["current_version_id_joined"]:
        data["currentVersion"] = {
            "id": row["current_version_id_joined"],
            "versionNo": row["current_version_no"],
            "name": row["current_version_name"],
            "kind": row["current_kind"],
            "mime": row["current_mime"],
            "size": row["current_size"],
            "sha256": row["current_sha256"],
            "scanStatus": row["current_scan_status"],
            "reviewStatus": row["current_review_status"],
            "reviewNote": _row_get(row, "current_review_note", ""),
            "createdAt": row["current_version_created_at"],
            "listing": (
                {
                    "id": _row_get(row, "current_listing_id"),
                    "pricePoints": _row_get(row, "current_listing_price", 0),
                    "status": _row_get(row, "current_listing_status"),
                }
                if _row_get(row, "current_listing_id") is not None
                else None
            ),
            "contentUrl": f"/api/library/resources/{row['id']}/versions/{row['current_version_id_joined']}/content",
        }
    if include_versions:
        versions = conn.execute(
            """
            SELECT v.*, l.id AS listing_id, l.price_points AS listing_price, l.status AS listing_status,
                   (SELECT rr.note FROM resource_reviews rr WHERE rr.resource_version_id = v.id ORDER BY rr.id DESC LIMIT 1) AS review_note
            FROM library_resource_versions v
            LEFT JOIN resource_listings l ON l.resource_version_id = v.id
            WHERE v.resource_id = ? ORDER BY v.version_no DESC
            """,
            (row["id"],),
        ).fetchall()
        data["versions"] = [_library_version_dict(version, include_content_url=True) for version in versions]
    return data


def _library_upload_metadata(file_name: str | None, content_type: str | None, data: bytes):
    name = _validate_file_name(file_name or "")
    kind, mime = _classify_file(name, content_type, data)
    limit = _file_size_limit(kind)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"资源超过 {limit // (1024 * 1024)}MB 上限")
    return name, kind, mime, hashlib.sha256(data).hexdigest()


def _enforce_library_quota(conn, owner_account_id: int, incoming_bytes: int, *, new_resource: bool = False) -> None:
    resource_count = conn.execute(
        "SELECT COUNT(*) AS c FROM library_resources WHERE owner_account_id = ? AND status != 'deleted'",
        (owner_account_id,),
    ).fetchone()["c"]
    if new_resource and resource_count >= MAX_LIBRARY_RESOURCES:
        raise _friend_error(
            "library_quota_exceeded",
            f"资源数量已达上限（{MAX_LIBRARY_RESOURCES}）",
            413,
            resourceCount=resource_count,
            maxResources=MAX_LIBRARY_RESOURCES,
        )
    total = conn.execute(
        """
        SELECT COALESCE(SUM(v.size), 0) AS total
        FROM library_resource_versions v
        JOIN library_resources r ON r.id = v.resource_id
        WHERE r.owner_account_id = ? AND r.status != 'deleted'
        """,
        (owner_account_id,),
    ).fetchone()["total"]
    if total + incoming_bytes > MAX_LIBRARY_TOTAL_BYTES:
        raise _friend_error(
            "library_quota_exceeded",
            "资源库存储配额已用尽",
            413,
            usedBytes=total,
            requestedBytes=incoming_bytes,
            maxBytes=MAX_LIBRARY_TOTAL_BYTES,
        )


def _library_relative_path(owner_id: int, resource_id: int, version_id: int, name: str) -> str:
    ext = _file_storage_ext(name)
    return f"{owner_id}/{resource_id}/{version_id}{ext}"


def _library_write_version(owner_id: int, resource_id: int, version_id: int, name: str, data: bytes) -> str:
    relative = _library_relative_path(owner_id, resource_id, version_id, name)
    destination = LIBRARY_DIR / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    _write_file_bytes(destination, data)
    return relative


@app.get("/api/library/resources")
def list_library_resources(user: CurrentUser):
    owner_id = _effective_human_id(user)
    with get_db() as conn:
        rows = conn.execute(
            LIBRARY_SELECT
            + " WHERE r.status != 'deleted' AND (r.owner_account_id = ? OR r.economic_owner_id = ?)"
            + " ORDER BY r.updated_at DESC, r.id DESC",
            (owner_id, owner_id),
        ).fetchall()
        resources = [_library_resource_dict(conn, row) for row in rows]
        total_bytes = conn.execute(
            """
            SELECT COALESCE(SUM(v.size), 0) AS total
            FROM library_resource_versions v
            JOIN library_resources r ON r.id = v.resource_id
            WHERE r.owner_account_id = ? AND r.status != 'deleted'
            """,
            (owner_id,),
        ).fetchone()["total"]
    return {
        "resources": resources,
        "quota": {
            "resourceCount": len(resources),
            "maxResources": MAX_LIBRARY_RESOURCES,
            "usedBytes": total_bytes,
            "maxBytes": MAX_LIBRARY_TOTAL_BYTES,
        },
    }


@app.post("/api/library/resources")
async def create_library_resource(
    user: CurrentUser,
    file: UploadFile = File(...),
    name: Annotated[str | None, Form()] = None,
    description: Annotated[str, Form()] = "",
):
    data = await file.read()
    filename, kind, mime, sha256 = _library_upload_metadata(name or file.filename, file.content_type, data)
    if len(description) > 500:
        raise HTTPException(status_code=422, detail="资源描述最多 500 字符")
    created_path: Path | None = None
    try:
        with get_db() as conn:
            account = conn.execute(
                "SELECT id, kind, owner_id FROM users WHERE id = ?", (user["id"],)
            ).fetchone()
            owner_id = account["owner_id"] if account["kind"] == "agent" and account["owner_id"] else account["id"]
            economic_owner_id = owner_id
            _enforce_library_quota(conn, owner_id, len(data), new_resource=True)
            cur = conn.execute(
                """
                INSERT INTO library_resources (owner_account_id, economic_owner_id, name, description)
                VALUES (?, ?, ?, ?)
                """,
                (owner_id, economic_owner_id, filename, description),
            )
            resource_id = cur.lastrowid
            version_cur = conn.execute(
                """
                INSERT INTO library_resource_versions
                    (resource_id, version_no, name, kind, mime, size, sha256, content_path, scan_status, created_by)
                VALUES (?, 1, ?, ?, ?, ?, ?, '', 'clean', ?)
                """,
                (resource_id, filename, kind, mime, len(data), sha256, user["id"]),
            )
            version_id = version_cur.lastrowid
            relative = _library_write_version(owner_id, resource_id, version_id, filename, data)
            created_path = LIBRARY_DIR / relative
            conn.execute(
                "UPDATE library_resource_versions SET content_path = ? WHERE id = ?",
                (relative, version_id),
            )
            conn.execute(
                "UPDATE library_resources SET current_version_id = ?, updated_at = datetime('now') WHERE id = ?",
                (version_id, resource_id),
            )
            row = _library_resource(conn, resource_id)
            result = _library_resource_dict(conn, row, include_versions=True)
    except Exception:
        if created_path:
            created_path.unlink(missing_ok=True)
        raise
    return result


@app.get("/api/library/resources/{resource_id}")
def get_library_resource(resource_id: int, user: CurrentUser):
    with get_db() as conn:
        row = _library_resource(conn, resource_id)
        if not row or row["status"] == "deleted" or not _library_can_manage(row, user):
            raise HTTPException(status_code=404, detail="资源不存在")
        return _library_resource_dict(conn, row, include_versions=True)


@app.patch("/api/library/resources/{resource_id}")
def update_library_resource(resource_id: int, body: LibraryResourceUpdate, user: CurrentUser):
    if body.name is None and body.description is None:
        raise HTTPException(status_code=400, detail="没有需要修改的字段")
    with get_db() as conn:
        row = _library_resource(conn, resource_id)
        if not row or row["status"] == "deleted" or not _library_can_manage(row, user):
            raise HTTPException(status_code=404, detail="资源不存在")
        name = _validate_file_name(body.name) if body.name is not None else row["name"]
        description = row["description"] if body.description is None else body.description
        conn.execute(
            "UPDATE library_resources SET name = ?, description = ?, updated_at = datetime('now') WHERE id = ?",
            (name, description, resource_id),
        )
        row = _library_resource(conn, resource_id)
        result = _library_resource_dict(conn, row, include_versions=True)
    return result


@app.delete("/api/library/resources/{resource_id}")
def archive_library_resource(resource_id: int, user: CurrentUser):
    with get_db() as conn:
        row = _library_resource(conn, resource_id)
        if not row or row["status"] == "deleted" or not _library_can_manage(row, user):
            raise HTTPException(status_code=404, detail="资源不存在")
        conn.execute(
            "UPDATE library_resources SET status = 'archived', deleted_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
            (resource_id,),
        )
    return {"archived": True, "resourceId": resource_id}


@app.post("/api/library/resources/{resource_id}/versions")
async def create_library_version(
    resource_id: int,
    user: CurrentUser,
    file: UploadFile = File(...),
    name: Annotated[str | None, Form()] = None,
):
    data = await file.read()
    with get_db() as conn:
        resource = _library_resource(conn, resource_id)
        if not resource or resource["status"] != "active" or not _library_can_manage(resource, user):
            raise HTTPException(status_code=404, detail="资源不存在或已归档")
        fallback_name = resource["current_version_name"] or resource["name"]
        filename, kind, mime, sha256 = _library_upload_metadata(name or file.filename or fallback_name, file.content_type, data)
        _enforce_library_quota(conn, resource["owner_account_id"], len(data))
        duplicate = conn.execute(
            "SELECT id FROM library_resource_versions WHERE resource_id = ? AND sha256 = ?",
            (resource_id, sha256),
        ).fetchone()
        if duplicate:
            raise _friend_error("resource_version_conflict", "该资源已经存在相同内容版本", 409, versionId=duplicate["id"])
        version_no = conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 AS next_no FROM library_resource_versions WHERE resource_id = ?",
            (resource_id,),
        ).fetchone()["next_no"]
        version_cur = conn.execute(
            """
            INSERT INTO library_resource_versions
                (resource_id, version_no, name, kind, mime, size, sha256, content_path, scan_status, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, '', 'clean', ?)
            """,
            (resource_id, version_no, filename, kind, mime, len(data), sha256, user["id"]),
        )
        version_id = version_cur.lastrowid
        owner_id = resource["owner_account_id"]
        relative = _library_relative_path(owner_id, resource_id, version_id, filename)
        destination = LIBRARY_DIR / relative
        try:
            _library_write_version(owner_id, resource_id, version_id, filename, data)
            conn.execute("UPDATE library_resource_versions SET content_path = ? WHERE id = ?", (relative, version_id))
            conn.execute(
                """
                UPDATE library_resources
                SET name = ?, current_version_id = ?, updated_at = datetime('now')
                WHERE id = ?
                """,
                (filename, version_id, resource_id),
            )
            row = _library_resource(conn, resource_id)
            result = _library_resource_dict(conn, row, include_versions=True)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
    return result


@app.get("/api/library/resources/{resource_id}/versions/{version_id}/content")
def download_library_version(resource_id: int, version_id: int, user: CurrentUser):
    with get_db() as conn:
        version = _library_version(conn, resource_id, version_id)
        if not version or version["resource_status"] == "deleted" or not (
            _library_can_manage(version, user) or user["isSuperadmin"]
        ):
            raise HTTPException(status_code=404, detail="资源版本不存在")
        path = LIBRARY_DIR / version["content_path"]
        if not path.is_file():
            raise HTTPException(status_code=404, detail="资源文件缺失")
        content_path = path
        filename = version["name"]
        mime = version["mime"]
    return _file_response(content_path, filename, inline=False, media_type=mime)




@app.post("/api/library/resources/{resource_id}/versions/{version_id}/submit")
def submit_resource_review(resource_id: int, version_id: int, user: CurrentUser):
    with get_db() as conn:
        resource = _library_resource(conn, resource_id)
        version = _library_version(conn, resource_id, version_id)
        if not resource or not version or not _library_can_manage(resource, user):
            raise HTTPException(status_code=404, detail="资源版本不存在")
        if version["scan_status"] not in ("clean", "pending"):
            raise _friend_error("moderation_required", "资源未通过安全扫描，不能提交审核", 409)
        conn.execute(
            "UPDATE library_resource_versions SET review_status = 'pending_review' WHERE id = ?",
            (version_id,),
        )
        conn.execute(
            """
            INSERT INTO resource_reviews (resource_version_id, reviewer_user_id, decision, note, scan_status)
            VALUES (?, ?, 'submitted', '', ?)
            """,
            (version_id, user["id"], version["scan_status"]),
        )
        row = _library_version(conn, resource_id, version_id)
    return {"version": _library_version_dict(row, include_content_url=True), "submitted": True}


@app.get("/api/admin/resource-reviews")
def list_resource_reviews(user: SuperAdmin):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT v.*, r.name AS resource_name, r.owner_account_id,
                   owner.username AS owner_name
            FROM library_resource_versions v
            JOIN library_resources r ON r.id = v.resource_id
            JOIN users owner ON owner.id = r.owner_account_id
            WHERE v.review_status = 'pending_review'
            ORDER BY v.created_at ASC, v.id ASC
            """
        ).fetchall()
    return {
        "reviews": [
            {
                **_library_version_dict(row, include_content_url=True),
                "resourceName": row["resource_name"],
                "ownerName": row["owner_name"],
            }
            for row in rows
        ]
    }


@app.post("/api/admin/resource-reviews/{version_id}")
def decide_resource_review(version_id: int, body: ResourceReviewDecision, user: SuperAdmin):
    with get_db() as conn:
        version = conn.execute(
            "SELECT * FROM library_resource_versions WHERE id = ?", (version_id,)
        ).fetchone()
        if not version:
            raise HTTPException(status_code=404, detail="资源版本不存在")
        if body.decision == "approved" and version["scan_status"] != "clean":
            # 当前服务尚未接入外部杀毒服务；上传时的基础魔数/大小校验将 pending
            # 标为 clean。未来异步扫描失败时仍可阻止批准。
            raise _friend_error("moderation_required", "资源尚未通过安全扫描", 409)
        conn.execute(
            "UPDATE library_resource_versions SET review_status = ? WHERE id = ?",
            (body.decision, version_id),
        )
        conn.execute(
            """
            INSERT INTO resource_reviews (resource_version_id, reviewer_user_id, decision, note, scan_status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (version_id, user["id"], body.decision, body.note, version["scan_status"]),
        )
        row = conn.execute("SELECT * FROM library_resource_versions WHERE id = ?", (version_id,)).fetchone()
    result = _library_version_dict(row, include_content_url=False)
    result["reviewNote"] = body.note
    return {"version": result, "decision": body.decision}


@app.post("/api/library/resources/{resource_id}/versions/{version_id}/listing")
def create_resource_listing(resource_id: int, version_id: int, body: ResourceListingCreate, user: CurrentUser):
    with get_db() as conn:
        resource = _library_resource(conn, resource_id)
        version = _library_version(conn, resource_id, version_id)
        if not resource or not version or not _library_can_manage(resource, user):
            raise HTTPException(status_code=404, detail="资源版本不存在")
        if resource["status"] != "active":
            raise _friend_error("resource_not_listed", "已归档资源不能上架", 409)
        if version["review_status"] != "approved" or version["scan_status"] != "clean":
            raise _friend_error("resource_not_approved", "资源版本尚未通过审核", 409)
        seller_id = resource["economic_owner_id"]
        existing = conn.execute(
            "SELECT * FROM resource_listings WHERE resource_version_id = ?", (version_id,)
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE resource_listings
                SET seller_user_id = ?, price_points = ?, status = 'listed', listed_at = COALESCE(listed_at, datetime('now')),
                    unlisted_at = NULL, updated_at = datetime('now')
                WHERE id = ?
                """,
                (seller_id, body.pricePoints, existing["id"]),
            )
            listing_id = existing["id"]
            idempotent = True
        else:
            cur = conn.execute(
                """
                INSERT INTO resource_listings (resource_version_id, seller_user_id, price_points, status, listed_at)
                VALUES (?, ?, ?, 'listed', datetime('now'))
                """,
                (version_id, seller_id, body.pricePoints),
            )
            listing_id = cur.lastrowid
            idempotent = False
        listing = conn.execute("SELECT * FROM resource_listings WHERE id = ?", (listing_id,)).fetchone()
    return {"listing": _listing_dict(listing), "idempotent": idempotent}


def _listing_dict(row) -> dict:
    return {
        "id": row["id"],
        "resourceVersionId": row["resource_version_id"],
        "sellerUserId": row["seller_user_id"],
        "pricePoints": row["price_points"],
        "status": row["status"],
        "listedAt": row["listed_at"],
        "unlistedAt": row["unlisted_at"],
        "updatedAt": row["updated_at"],
    }


@app.patch("/api/library/listings/{listing_id}")
def update_resource_listing(listing_id: int, body: ResourceListingUpdate, user: CurrentUser):
    if body.pricePoints is None and body.listed is None:
        raise HTTPException(status_code=400, detail="没有需要修改的字段")
    with get_db() as conn:
        listing = conn.execute(
            """
            SELECT l.*, r.owner_account_id, r.economic_owner_id, v.review_status, v.scan_status
            FROM resource_listings l
            JOIN library_resource_versions v ON v.id = l.resource_version_id
            JOIN library_resources r ON r.id = v.resource_id
            WHERE l.id = ?
            """,
            (listing_id,),
        ).fetchone()
        if not listing or not _library_can_manage(listing, user):
            raise HTTPException(status_code=404, detail="资源 listing 不存在")
        price = listing["price_points"] if body.pricePoints is None else body.pricePoints
        status = listing["status"]
        if body.listed is not None:
            if body.listed:
                if listing["review_status"] != "approved" or listing["scan_status"] != "clean":
                    raise _friend_error("resource_not_approved", "资源版本尚未通过审核", 409)
                status = "listed"
            else:
                status = "withdrawn"
        conn.execute(
            """
            UPDATE resource_listings
            SET price_points = ?, status = ?, listed_at = CASE WHEN ? = 'listed' THEN COALESCE(listed_at, datetime('now')) ELSE listed_at END,
                unlisted_at = CASE WHEN ? != 'listed' THEN datetime('now') ELSE unlisted_at END,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (price, status, status, status, listing_id),
        )
        listing = conn.execute("SELECT * FROM resource_listings WHERE id = ?", (listing_id,)).fetchone()
    return {"listing": _listing_dict(listing)}


@app.delete("/api/library/listings/{listing_id}")
def delete_resource_listing(listing_id: int, user: CurrentUser):
    with get_db() as conn:
        listing = conn.execute(
            """
            SELECT l.*, r.owner_account_id, r.economic_owner_id
            FROM resource_listings l
            JOIN library_resource_versions v ON v.id = l.resource_version_id
            JOIN library_resources r ON r.id = v.resource_id
            WHERE l.id = ?
            """,
            (listing_id,),
        ).fetchone()
        if not listing or not _library_can_manage(listing, user):
            raise HTTPException(status_code=404, detail="资源 listing 不存在")
        conn.execute(
            "UPDATE resource_listings SET status = 'withdrawn', unlisted_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
            (listing_id,),
        )
    return {"unlisted": True, "listingId": listing_id}


STORE_SELECT = """
    SELECT l.*, v.resource_id, v.version_no, v.name AS version_name, v.kind, v.mime, v.size,
           v.sha256, v.scan_status, v.review_status,
           r.name AS resource_name, r.description, r.owner_account_id,
           seller.username AS seller_name
    FROM resource_listings l
    JOIN library_resource_versions v ON v.id = l.resource_version_id
    JOIN library_resources r ON r.id = v.resource_id
    JOIN users seller ON seller.id = l.seller_user_id
"""


def _store_dict(row) -> dict:
    return {
        "listingId": row["id"],
        "resourceId": row["resource_id"],
        "resourceVersionId": row["resource_version_id"],
        "resourceName": row["resource_name"],
        "name": row["version_name"],
        "description": row["description"],
        "kind": row["kind"],
        "mime": row["mime"],
        "size": row["size"],
        "versionNo": row["version_no"],
        "seller": row["seller_name"],
        "pricePoints": row["price_points"],
        "status": row["status"],
        "listedAt": row["listed_at"],
        "updatedAt": row["updated_at"],
    }


@app.get("/api/store/resources")
def list_store_resources(
    user: CurrentUser,
    kind: str | None = None,
    q: str | None = None,
):
    clauses = ["l.status = 'listed'", "v.review_status = 'approved'", "v.scan_status = 'clean'", "r.status = 'active'"]
    params: list = []
    if kind:
        clauses.append("v.kind = ?")
        params.append(kind)
    if q:
        clauses.append("(r.name LIKE ? OR r.description LIKE ? OR v.name LIKE ?)")
        pattern = f"%{q}%"
        params.extend((pattern, pattern, pattern))
    with get_db() as conn:
        rows = conn.execute(STORE_SELECT + " WHERE " + " AND ".join(clauses) + " ORDER BY l.listed_at DESC, l.id DESC", params).fetchall()
    return {"resources": [_store_dict(row) for row in rows]}


@app.get("/api/store/resources/{resource_id}")
def get_store_resource(resource_id: int, user: CurrentUser):
    with get_db() as conn:
        rows = conn.execute(
            STORE_SELECT
            + " WHERE l.status = 'listed' AND v.review_status = 'approved' AND v.scan_status = 'clean'"
            + " AND r.status = 'active' AND r.id = ? ORDER BY v.version_no DESC",
            (resource_id,),
        ).fetchall()
    if not rows:
        raise _friend_error("resource_not_listed", "资源未上架", 404)
    return {"resourceId": resource_id, "listings": [_store_dict(row) for row in rows]}


def _economic_user_id(conn, user_id: int) -> int:
    row = conn.execute("SELECT id, kind, owner_id FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="账号不存在")
    return int(row["owner_id"] if row["kind"] == "agent" and row["owner_id"] else row["id"])


def _ensure_points_account(conn, user_id: int):
    conn.execute(
        "INSERT INTO point_accounts (user_id, balance) VALUES (?, 0) ON CONFLICT(user_id) DO NOTHING",
        (user_id,),
    )


@app.post("/api/admin/points/grants")
def grant_campaign_points(body: PointsCampaignGrant, user: SuperAdmin):
    reason = body.reason.strip()
    if not reason:
        raise HTTPException(status_code=422, detail="活动发放原因不能为空")
    with get_db() as conn:
        target = conn.execute(
            "SELECT id, username, kind, status FROM users WHERE username = ? COLLATE NOCASE",
            (body.username,),
        ).fetchone()
        if not target or target["kind"] != "human" or target["status"] != "active":
            raise _friend_error("points_grant_target_not_found", "活动积分只能发放给启用中的人类账号", 404)
        key = f"campaign:{body.campaignKey}:user:{target['id']}"
        granted = _grant_points(
            conn,
            user_id=target["id"],
            amount=body.amount,
            transaction_type="campaign_grant",
            idempotency_key=key,
            reason=reason,
            campaign_key=body.campaignKey,
        )
        balance = conn.execute(
            "SELECT balance FROM point_accounts WHERE user_id = ?", (target["id"],)
        ).fetchone()["balance"]
        transaction = conn.execute(
            "SELECT id FROM point_transactions WHERE idempotency_key = ?", (key,)
        ).fetchone()
    return {
        "username": target["username"],
        "amount": body.amount,
        "balance": balance,
        "transactionId": transaction["id"],
        "campaignKey": body.campaignKey,
        "idempotent": not granted,
    }


@app.get("/api/points")
def get_points(user: CurrentUser):
    with get_db() as conn:
        economic_id = _economic_user_id(conn, user["id"])
        _ensure_points_account(conn, economic_id)
        row = conn.execute("SELECT user_id, balance, updated_at FROM point_accounts WHERE user_id = ?", (economic_id,)).fetchone()
    return {"userId": row["user_id"], "balance": row["balance"], "updatedAt": row["updated_at"]}


@app.get("/api/points/transactions")
def list_point_transactions(user: CurrentUser):
    with get_db() as conn:
        economic_id = _economic_user_id(conn, user["id"])
        rows = conn.execute(
            """
            SELECT * FROM point_transactions
            WHERE payer_user_id = ? OR payee_user_id = ?
            ORDER BY created_at DESC, id DESC LIMIT 200
            """,
            (economic_id, economic_id),
        ).fetchall()
    return {
        "transactions": [
            {
                "id": row["id"],
                "idempotencyKey": row["idempotency_key"],
                "payerUserId": row["payer_user_id"],
                "payeeUserId": row["payee_user_id"],
                "amount": row["amount"],
                "transactionType": row["transaction_type"],
                "metadata": json.loads(row["metadata"] or "{}"),
                "resourceId": row["resource_id"],
                "resourceVersionId": row["resource_version_id"],
                "roomId": row["room_id"],
                "status": row["status"],
                "createdAt": row["created_at"],
            }
            for row in rows
        ]
    }


@app.post("/api/rooms/{room_ref}/files/from-library")
def import_library_resource(room_ref: str, body: LibraryImportRequest, user: CurrentUser):
    created_path: Path | None = None
    with get_db() as conn:
        room, member = _require_membership(conn, room_ref, user["id"])
        _check_file_edit_allowed(room, member, user["id"])
        resource = _library_resource(conn, body.resourceId)
        version = _library_version(conn, body.resourceId, body.resourceVersionId)
        if not resource or not version or resource["status"] == "deleted":
            raise _friend_error("resource_not_found", "资源版本不存在", 404)
        if not _library_can_manage(resource, user) and resource["status"] != "active":
            raise _friend_error("resource_not_found", "资源版本不存在", 404)
        purchaser_id = _economic_user_id(conn, user["id"])
        existing_license = conn.execute(
            """
            SELECT l.*, f.name AS file_name
            FROM room_resource_licenses l
            LEFT JOIN room_files f ON f.id = l.room_file_id
            WHERE l.room_id = ? AND l.purchaser_user_id = ? AND l.resource_version_id = ?
            """,
            (room["id"], purchaser_id, body.resourceVersionId),
        ).fetchone()
        if existing_license and existing_license["room_file_id"]:
            return {
                "roomId": room["id"],
                "roomKey": _room_key(room),
                "licenseId": existing_license["id"],
                "fileId": existing_license["room_file_id"],
                "charged": False,
                "idempotent": True,
            }
        is_owner = _library_can_manage(resource, user)
        listing = None
        if is_owner:
            price = 0
        else:
            listing = conn.execute(
                STORE_SELECT + " WHERE l.id = ? AND l.resource_version_id = ? AND l.status = 'listed'",
                (body.listingId, body.resourceVersionId),
            ).fetchone() if body.listingId else None
            if not listing or listing["review_status"] != "approved" or listing["scan_status"] != "clean":
                raise _friend_error("resource_not_listed", "资源未上架或 listing 已失效", 403)
            price = int(listing["price_points"])
        target_name = _validate_file_name(body.name or version["name"])
        existing_file = conn.execute(
            "SELECT id FROM room_files WHERE room_id = ? AND name = ? COLLATE NOCASE",
            (room["id"], target_name),
        ).fetchone()
        if existing_file:
            raise _friend_error("room_file_name_conflict", "房间中已存在同名共同文件", 409)
        source_path = LIBRARY_DIR / version["content_path"]
        if not source_path.is_file():
            raise HTTPException(status_code=404, detail="资源文件缺失")
        seller_id = resource["economic_owner_id"]
        idempotency_key = body.idempotencyKey or f"room-import:{room['id']}:{purchaser_id}:{body.resourceVersionId}"
        prior_transaction = conn.execute(
            "SELECT * FROM point_transactions WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if prior_transaction:
            raise _friend_error("license_exists", "该导入请求已经处理", 409, transactionId=prior_transaction["id"])
        if price and purchaser_id == seller_id:
            price = 0
        if price:
            _ensure_points_account(conn, purchaser_id)
            _ensure_points_account(conn, seller_id)
            updated = conn.execute(
                "UPDATE point_accounts SET balance = balance - ?, updated_at = datetime('now') WHERE user_id = ? AND balance >= ?",
                (price, purchaser_id, price),
            )
            if updated.rowcount != 1:
                raise _friend_error("insufficient_points", "积分不足", 402, required=price)
            conn.execute(
                "UPDATE point_accounts SET balance = balance + ?, updated_at = datetime('now') WHERE user_id = ?",
                (price, seller_id),
            )
        elif purchaser_id != seller_id:
            _ensure_points_account(conn, purchaser_id)
            _ensure_points_account(conn, seller_id)
        now = _db_now(conn)
        conn.execute(
            """
            INSERT INTO point_transactions
                (idempotency_key, payer_user_id, payee_user_id, amount, resource_id, resource_version_id, room_id, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'committed')
            """,
            (idempotency_key, purchaser_id, seller_id, price, body.resourceId, body.resourceVersionId, room["id"]),
        )
        transaction_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute(
            """
            INSERT INTO room_files (
                room_id, name, kind, mime, size, description, created_by, created_at, updated_by, updated_at,
                source_resource_id, source_resource_version_id, source_purchased_by, source_detached
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                room["id"], target_name, version["kind"], version["mime"], version["size"], resource["description"],
                user["id"], now, user["id"], now, body.resourceId, body.resourceVersionId, user["id"],
            ),
        )
        file_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        rel_path = f"{room['id']}/{file_id}{_file_storage_ext(target_name)}"
        destination = FILES_DIR / rel_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            _write_file_bytes(destination, source_path.read_bytes())
            created_path = destination
            conn.execute("UPDATE room_files SET content_path = ? WHERE id = ?", (rel_path, file_id))
            conn.execute(
                """
                INSERT INTO room_resource_licenses
                    (room_id, purchaser_user_id, resource_id, resource_version_id, listing_id, price_points, transaction_id, room_file_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (room["id"], purchaser_id, body.resourceId, body.resourceVersionId, listing["id"] if listing else None, price, transaction_id, file_id),
            )
            license_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            conn.execute("UPDATE point_transactions SET license_id = ? WHERE id = ?", (license_id, transaction_id))
            conn.execute("UPDATE room_files SET source_license_id = ? WHERE id = ?", (license_id, file_id))
            _bump_files_revision(conn, room["id"])
            payload = _single_file_payload(conn, room["name"], room["id"], file_id)
            room_id = room["id"]
        except Exception:
            destination.unlink(missing_ok=True)
            raise
    notify_room(room_id)
    payload.update({"licenseId": license_id, "charged": bool(price), "pricePoints": price, "idempotent": False})
    return payload

# ---------- 好友关系与 private 房间邀请 ----------

def _friend_pair(user_id: int, other_id: int) -> tuple[int, int]:
    return (user_id, other_id) if user_id < other_id else (other_id, user_id)


def _get_friendship(conn, user_id: int, other_id: int):
    a, b = _friend_pair(user_id, other_id)
    return conn.execute(
        "SELECT * FROM user_friendships WHERE user_a_id = ? AND user_b_id = ?",
        (a, b),
    ).fetchone()


def _pair_ids(user_a_id: int, user_b_id: int) -> tuple[int, int]:
    return (user_a_id, user_b_id) if user_a_id < user_b_id else (user_b_id, user_a_id)


def _direct_pair_room(conn, user_a_id: int, user_b_id: int):
    a, b = _pair_ids(user_a_id, user_b_id)
    return conn.execute(
        ROOM_SELECT
        + " WHERE r.room_type = 'direct_pair' AND r.pair_user_a_id = ? AND r.pair_user_b_id = ?"
        + " AND r.archived_at IS NULL",
        (a, b),
    ).fetchone()


def _ensure_direct_pair_room(conn, user_a_id: int, user_b_id: int):
    """好友接受事务内创建/复用双人房；调用方已保证双方是 accepted human。"""
    a, b = _pair_ids(user_a_id, user_b_id)
    existing = _direct_pair_room(conn, a, b)
    if existing:
        for member_id in (a, b):
            conn.execute(
                """
                INSERT INTO room_members (room_id, user_id)
                VALUES (?, ?)
                ON CONFLICT(room_id, user_id) DO UPDATE SET last_seen_at = datetime('now')
                """,
                (existing["id"], member_id),
            )
        return existing, False
    owner = _user_by_id(conn, a)
    base_name = f"与{_user_by_id(conn, b)['username']}的私聊"
    name = base_name
    suffix = 2
    while conn.execute(
        "SELECT 1 FROM rooms WHERE namespace_owner_id = ? AND name = ? COLLATE NOCASE AND archived_at IS NULL",
        (a, name),
    ).fetchone():
        name = f"{base_name} {suffix}"
        suffix += 1
    try:
        cur = conn.execute(
            """
            INSERT INTO rooms (
                name, created_by, namespace_owner_id, room_type, pair_user_a_id, pair_user_b_id,
                password_hash, visibility
            )
            VALUES (?, ?, ?, 'direct_pair', ?, ?, NULL, 'private')
            """,
            (name, a, a, a, b),
        )
    except sqlite3.IntegrityError:
        existing = _direct_pair_room(conn, a, b)
        if not existing:
            raise
        return existing, False
    room = _get_room_by_id(conn, cur.lastrowid)
    watermark = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS w FROM messages WHERE room_id = ?", (room["id"],)
    ).fetchone()["w"]
    for member_id in (a, b):
        conn.execute(
            """
            INSERT INTO room_members (room_id, user_id, first_visible_msg_id, last_read_msg_id)
            VALUES (?, ?, ?, ?)
            """,
            (room["id"], member_id, watermark, watermark),
        )
    return room, True


def _are_friends(conn, user_id: int, other_id: int) -> bool:
    row = _get_friendship(conn, user_id, other_id)
    return bool(row and row["status"] == "accepted")


def _friendship_dict(conn, row, viewer_id: int) -> dict:
    other_id = row["user_b_id"] if row["user_a_id"] == viewer_id else row["user_a_id"]
    other = conn.execute(
        """
        SELECT id, username, kind, status, avatar_updated_at
        FROM users WHERE id = ?
        """,
        (other_id,),
    ).fetchone()
    return {
        "id": row["id"],
        "username": other["username"],
        "kind": other["kind"],
        "status": row["status"],
        "requestedBy": row["requested_by"],
        "incoming": row["requested_by"] != viewer_id,
        "avatarUrl": _avatar_url(other["username"], other["avatar_updated_at"]),
        "createdAt": row["created_at"],
        "respondedAt": row["responded_at"],
        "updatedAt": row["updated_at"],
    }


def _friend_error(code: str, message: str, status_code: int = 409, **extra) -> HTTPException:
    detail = {"code": code, "message": message}
    detail.update(extra)
    return HTTPException(status_code=status_code, detail=detail)


def _room_namespace_owner_id(room) -> int:
    return int(room["namespace_owner_id"] or room["created_by"])


def _room_namespace_owner_is_human(room) -> bool:
    # namespace_owner_id 已由迁移回填；creatorKind 仅作旧库/测试行的安全兜底。
    return room["namespaceOwnerName"] is not None and (
        room["creatorKind"] == "human" or room["namespace_owner_id"] != room["created_by"]
    )


def _has_accepted_room_invite(conn, room_id: int, user_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM room_invites
        WHERE room_id = ? AND invitee_user_id = ? AND status = 'accepted'
          AND (expires_at IS NULL OR expires_at > datetime('now'))
        LIMIT 1
        """,
        (room_id, user_id),
    ).fetchone()
    return bool(row)


def _require_private_join_access(conn, room, user: dict) -> None:
    """private 新成员的好友/邀请门槛；房间治理者与建房 Agent 维持旧语义。"""
    if room["visibility"] != "private":
        return
    if _row_get(room, "room_type", "standard") == "direct_pair":
        effective_id = _effective_human_id(user)
        if effective_id not in (room["pair_user_a_id"], room["pair_user_b_id"]):
            raise _friend_error("pair_member_required", "该房间只允许双人好友及其 Agent 加入", 403)
        other_id = room["pair_user_b_id"] if effective_id == room["pair_user_a_id"] else room["pair_user_a_id"]
        if not _are_friends(conn, effective_id, other_id):
            raise _friend_error("friend_required", "重新加入双人房前需要先恢复好友关系", 403)
        return
    if _is_room_governor(room, user["id"]):
        return
    namespace_owner_id = _room_namespace_owner_id(room)
    if user["kind"] != "human":
        raise _friend_error("friend_or_invite_required", "private 房间需要好友关系和已接受的邀请", 403)
    if not _are_friends(conn, namespace_owner_id, user["id"]):
        raise _friend_error("friend_or_invite_required", "你不是该 private 房间命名空间所有者的好友", 403)
    if not _has_accepted_room_invite(conn, room["id"], user["id"]):
        raise _friend_error("invite_required", "请先接受房主发出的房间邀请", 403)


def _room_for_ref(conn, room_ref: str, user_id: int | None = None):
    if room_ref.isdigit():
        return _get_room_by_id(conn, int(room_ref))
    if ":" in room_ref:
        return _get_room_by_key(conn, room_ref)
    return _get_room(conn, room_ref, user_id=user_id)


@app.post("/api/friends/requests")
def create_friend_request(body: FriendRequestCreate, user: HumanUser):
    with get_db() as conn:
        target = _get_user(conn, body.username)
        if not target or target["kind"] != "human" or target["status"] != "active":
            raise _friend_error("friend_target_not_found", "找不到可添加的人类用户", 404)
        if target["id"] == user["id"]:
            raise _friend_error("friend_self_forbidden", "不能添加自己为好友", 400)
        row = _get_friendship(conn, user["id"], target["id"])
        if row and row["status"] == "accepted":
            raise _friend_error("already_friends", "你们已经是好友", 409)
        if row and row["status"] == "pending":
            return {"request": _friendship_dict(conn, row, user["id"]), "idempotent": True}
        a, b = _friend_pair(user["id"], target["id"])
        if row:
            conn.execute(
                """
                UPDATE user_friendships
                SET status = 'pending', requested_by = ?, responded_at = NULL, updated_at = datetime('now')
                WHERE id = ?
                """,
                (user["id"], row["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO user_friendships
                    (user_a_id, user_b_id, status, requested_by)
                VALUES (?, ?, 'pending', ?)
                """,
                (a, b, user["id"]),
            )
        row = _get_friendship(conn, user["id"], target["id"])
        result = _friendship_dict(conn, row, user["id"])
    return {"request": result, "idempotent": False}


@app.get("/api/friends")
def list_friends(
    user: CurrentUser,
    status: Annotated[str, Query(pattern="^(accepted|incoming|outgoing|all)$")] = "accepted",
):
    viewer_id = _effective_human_id(user)
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM user_friendships
            WHERE user_a_id = ? OR user_b_id = ?
            ORDER BY updated_at DESC, id DESC
            """,
            (viewer_id, viewer_id),
        ).fetchall()
        result = []
        for row in rows:
            incoming = row["requested_by"] != viewer_id
            if status == "accepted" and row["status"] != "accepted":
                continue
            if status == "incoming" and (row["status"] != "pending" or not incoming):
                continue
            if status == "outgoing" and (row["status"] != "pending" or incoming):
                continue
            result.append(_friendship_dict(conn, row, viewer_id))
    return {"friends": result}


def _load_friend_request(conn, request_id: int):
    return conn.execute("SELECT * FROM user_friendships WHERE id = ?", (request_id,)).fetchone()


@app.post("/api/friends/requests/{request_id}/accept")
def accept_friend_request(request_id: int, user: HumanUser):
    with get_db() as conn:
        row = _load_friend_request(conn, request_id)
        if not row or user["id"] not in (row["user_a_id"], row["user_b_id"]):
            raise _friend_error("friend_request_not_found", "好友请求不存在", 404)
        if row["status"] == "accepted":
            pair_room, pair_created = _ensure_direct_pair_room(conn, row["user_a_id"], row["user_b_id"])
            pair_payload = {**_room_dict(pair_room, user["id"]), "created": pair_created, "joined": True}
            return {
                "friendship": _friendship_dict(conn, row, user["id"]),
                "pairRoom": pair_payload,
                "idempotent": True,
            }
        if row["status"] != "pending" or row["requested_by"] == user["id"]:
            raise _friend_error("friend_request_not_actionable", "该好友请求不能由当前用户接受", 409)
        conn.execute(
            "UPDATE user_friendships SET status = 'accepted', responded_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
            (request_id,),
        )
        row = _load_friend_request(conn, request_id)
        result = _friendship_dict(conn, row, user["id"])
        pair_room, pair_created = _ensure_direct_pair_room(conn, row["user_a_id"], row["user_b_id"])
        pair_payload = {**_room_dict(pair_room, user["id"]), "created": pair_created, "joined": True}
    return {"friendship": result, "pairRoom": pair_payload, "idempotent": False}


@app.post("/api/friends/requests/{request_id}/decline")
def decline_friend_request(request_id: int, user: HumanUser):
    with get_db() as conn:
        row = _load_friend_request(conn, request_id)
        if not row or user["id"] not in (row["user_a_id"], row["user_b_id"]):
            raise _friend_error("friend_request_not_found", "好友请求不存在", 404)
        if row["status"] != "pending":
            return {"friendship": _friendship_dict(conn, row, user["id"]), "idempotent": True}
        if row["requested_by"] == user["id"]:
            raise _friend_error("friend_request_not_actionable", "发出方不能拒绝自己的好友请求", 409)
        conn.execute(
            "UPDATE user_friendships SET status = 'declined', responded_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
            (request_id,),
        )
        row = _load_friend_request(conn, request_id)
        result = _friendship_dict(conn, row, user["id"])
    return {"friendship": result, "idempotent": False}


@app.delete("/api/friends/requests/{request_id}")
def cancel_friend_request(request_id: int, user: HumanUser):
    with get_db() as conn:
        row = _load_friend_request(conn, request_id)
        if not row or row["requested_by"] != user["id"]:
            raise _friend_error("friend_request_not_found", "可取消的好友请求不存在", 404)
        if row["status"] == "pending":
            conn.execute(
                "UPDATE user_friendships SET status = 'revoked', responded_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
                (request_id,),
            )
            row = _load_friend_request(conn, request_id)
    return {"friendship": _friendship_dict(conn, row, user["id"]), "idempotent": True}


@app.delete("/api/friends/{username}")
def remove_friend(username: str, user: HumanUser):
    with get_db() as conn:
        target = _get_user(conn, username)
        if not target or target["kind"] != "human":
            raise _friend_error("friend_not_found", "好友不存在", 404)
        row = _get_friendship(conn, user["id"], target["id"])
        if not row or row["status"] != "accepted":
            return {"removed": True, "idempotent": True}
        conn.execute(
            "UPDATE user_friendships SET status = 'revoked', responded_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
            (row["id"],),
        )
        conn.execute(
            """
            UPDATE room_invites SET status = 'revoked', updated_at = datetime('now')
            WHERE status = 'pending'
              AND ((inviter_user_id = ? AND invitee_user_id = ?)
                   OR (inviter_user_id = ? AND invitee_user_id = ?))
            """,
            (user["id"], target["id"], target["id"], user["id"]),
        )
    return {"removed": True, "idempotent": False}


def _require_invite_manager(conn, room_ref: str, user: dict):
    room = _room_for_ref(conn, room_ref, user_id=user["id"])
    if not room or _is_ended(room):
        raise HTTPException(status_code=404, detail="房间不存在，请先创建或加入")
    if user["kind"] != "human" or _room_namespace_owner_id(room) != user["id"]:
        raise HTTPException(status_code=403, detail="只有 private 房间命名空间所有者可以发出邀请")
    if room["visibility"] != "private":
        raise HTTPException(status_code=400, detail="只有 private 房间支持好友邀请")
    if _row_get(room, "room_type", "standard") == "direct_pair":
        raise _friend_error("pair_room_only", "双人好友房不能邀请其他人", 403)
    return room


@app.get("/api/room-invites")
def list_my_room_invites(user: HumanUser):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT i.*, r.name AS room_name, r.namespace_owner_id,
                   u.username AS inviter_name, no.username AS namespace_owner_name
            FROM room_invites i
            JOIN rooms r ON r.id = i.room_id
            JOIN users u ON u.id = i.inviter_user_id
            LEFT JOIN users no ON no.id = r.namespace_owner_id
            WHERE i.invitee_user_id = ?
            ORDER BY i.created_at DESC, i.id DESC
            """,
            (user["id"],),
        ).fetchall()
    return {
        "invites": [
            {
                "id": row["id"],
                "roomId": row["room_id"],
                "roomName": row["room_name"],
                "roomKey": f"{row['namespace_owner_name']}:{row['room_name']}",
                "inviter": row["inviter_name"],
                "namespaceOwner": row["namespace_owner_name"],
                "status": row["status"],
                "createdAt": row["created_at"],
                "respondedAt": row["responded_at"],
                "expiresAt": row["expires_at"],
            }
            for row in rows
        ]
    }


def _invite_dict(row) -> dict:
    return {
        "id": row["id"],
        "roomId": row["room_id"],
        "invitee": row["invitee_name"],
        "inviter": row["inviter_name"],
        "status": row["status"],
        "createdAt": row["created_at"],
        "respondedAt": row["responded_at"],
        "expiresAt": row["expires_at"],
    }


@app.get("/api/rooms/{room_ref}/invites")
def list_room_invites(room_ref: str, user: HumanUser):
    with get_db() as conn:
        room = _require_invite_manager(conn, room_ref, user)
        rows = conn.execute(
            """
            SELECT i.*, iu.username AS invitee_name, u.username AS inviter_name
            FROM room_invites i
            JOIN users iu ON iu.id = i.invitee_user_id
            JOIN users u ON u.id = i.inviter_user_id
            WHERE i.room_id = ? ORDER BY i.created_at DESC, i.id DESC
            """,
            (room["id"],),
        ).fetchall()
    return {"roomId": room["id"], "roomKey": _room_key(room), "invites": [_invite_dict(row) for row in rows]}


@app.post("/api/rooms/{room_ref}/invites")
def create_room_invite(room_ref: str, body: InviteCreate, user: HumanUser):
    with get_db() as conn:
        room = _require_invite_manager(conn, room_ref, user)
        target = _get_user(conn, body.username)
        if not target or target["kind"] != "human" or target["status"] != "active":
            raise _friend_error("invite_target_not_found", "邀请目标不存在", 404)
        if target["id"] == user["id"]:
            raise _friend_error("invite_self_forbidden", "不能邀请自己", 400)
        if not _are_friends(conn, user["id"], target["id"]):
            raise _friend_error("friend_required", "只能邀请已接受的好友", 403)
        existing = conn.execute(
            "SELECT id, status FROM room_invites WHERE room_id = ? AND invitee_user_id = ? AND status = 'pending'",
            (room["id"], target["id"]),
        ).fetchone()
        if existing:
            row = conn.execute(
                """
                SELECT i.*, iu.username AS invitee_name, u.username AS inviter_name
                FROM room_invites i JOIN users iu ON iu.id = i.invitee_user_id JOIN users u ON u.id = i.inviter_user_id
                WHERE i.id = ?
                """,
                (existing["id"],),
            ).fetchone()
            return {"invite": _invite_dict(row), "idempotent": True}
        try:
            cur = conn.execute(
                """
                INSERT INTO room_invites (room_id, inviter_user_id, invitee_user_id, status)
                VALUES (?, ?, ?, 'pending')
                """,
                (room["id"], user["id"], target["id"]),
            )
        except sqlite3.IntegrityError as exc:
            raise _friend_error("invite_pending", "该用户已有待处理邀请", 409) from exc
        row = conn.execute(
            """
            SELECT i.*, iu.username AS invitee_name, u.username AS inviter_name
            FROM room_invites i JOIN users iu ON iu.id = i.invitee_user_id JOIN users u ON u.id = i.inviter_user_id
            WHERE i.id = ?
            """,
            (cur.lastrowid,),
        ).fetchone()
    return {"invite": _invite_dict(row), "idempotent": False}


def _load_invite_for_user(conn, invite_id: int, user_id: int):
    return conn.execute(
        """
        SELECT i.*, iu.username AS invitee_name, u.username AS inviter_name
        FROM room_invites i JOIN users iu ON iu.id = i.invitee_user_id JOIN users u ON u.id = i.inviter_user_id
        WHERE i.id = ? AND i.invitee_user_id = ?
        """,
        (invite_id, user_id),
    ).fetchone()


@app.post("/api/room-invites/{invite_id}/accept")
def accept_room_invite(invite_id: int, user: HumanUser):
    with get_db() as conn:
        row = _load_invite_for_user(conn, invite_id, user["id"])
        if not row:
            raise _friend_error("invite_not_found", "房间邀请不存在", 404)
        if row["status"] == "accepted":
            return {"invite": _invite_dict(row), "idempotent": True}
        if row["status"] != "pending":
            raise _friend_error("invite_expired", "该房间邀请已失效", 403)
        if not _are_friends(conn, user["id"], row["inviter_user_id"]):
            conn.execute(
                "UPDATE room_invites SET status = 'revoked', updated_at = datetime('now') WHERE id = ?",
                (invite_id,),
            )
            raise _friend_error("friend_required", "好友关系已失效，不能接受邀请", 403)
        conn.execute(
            "UPDATE room_invites SET status = 'accepted', responded_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
            (invite_id,),
        )
        row = _load_invite_for_user(conn, invite_id, user["id"])
    return {"invite": _invite_dict(row), "idempotent": False}


@app.post("/api/room-invites/{invite_id}/decline")
def decline_room_invite(invite_id: int, user: HumanUser):
    with get_db() as conn:
        row = _load_invite_for_user(conn, invite_id, user["id"])
        if not row:
            raise _friend_error("invite_not_found", "房间邀请不存在", 404)
        if row["status"] == "pending":
            conn.execute(
                "UPDATE room_invites SET status = 'declined', responded_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
                (invite_id,),
            )
            row = _load_invite_for_user(conn, invite_id, user["id"])
    return {"invite": _invite_dict(row), "idempotent": True}


@app.delete("/api/rooms/{room_ref}/invites/{invite_id}")
def revoke_room_invite(room_ref: str, invite_id: int, user: HumanUser):
    with get_db() as conn:
        room = _require_invite_manager(conn, room_ref, user)
        cur = conn.execute(
            """
            UPDATE room_invites SET status = 'revoked', updated_at = datetime('now')
            WHERE id = ? AND room_id = ? AND status = 'pending'
            """,
            (invite_id, room["id"]),
        )
        if cur.rowcount == 0:
            row = conn.execute("SELECT status FROM room_invites WHERE id = ? AND room_id = ?", (invite_id, room["id"])).fetchone()
            if not row:
                raise _friend_error("invite_not_found", "房间邀请不存在", 404)
    return {"revoked": True, "idempotent": cur.rowcount == 0}


@app.post("/api/rooms/{room_ref}/agents")
def join_own_agent(room_ref: str, body: RoomAgentJoinRequest, user: HumanUser):
    with get_db() as conn:
        room = _room_for_ref(conn, room_ref, user_id=user["id"])
        if not room or _is_ended(room):
            raise HTTPException(status_code=404, detail="房间不存在，请先创建或加入")
        _reject_if_banned(conn, room, user["id"])
        if not _member(conn, room["id"], user["id"]):
            raise HTTPException(status_code=403, detail="必须先加入房间，才能带入自己的 Agent")
        agent = _get_user(conn, body.agentUsername)
        if not agent or agent["kind"] != "agent" or agent["owner_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="只能带入自己名下的 Agent")
        if agent["status"] != "active":
            raise HTTPException(status_code=403, detail="该 Agent 已停用")
        _reject_if_banned(conn, room, agent["id"])
        existing = _member(conn, room["id"], agent["id"])
        if existing:
            return {"roomId": room["id"], "roomKey": _room_key(room), "username": agent["username"], "joined": True, "idempotent": True}
        watermark = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS w FROM messages WHERE room_id = ?", (room["id"],)
        ).fetchone()["w"]
        conn.execute(
            """
            INSERT INTO room_members (room_id, user_id, last_seen_at, first_visible_msg_id, last_read_msg_id)
            VALUES (?, ?, datetime('now'), ?, ?)
            """,
            (room["id"], agent["id"], watermark, watermark),
        )
    notify_room(room["id"])
    return {"roomId": room["id"], "roomKey": _room_key(room), "username": agent["username"], "joined": True, "idempotent": False}


# ---------- 房间 ----------

@app.post("/api/rooms")
def join_or_create_room(body: RoomRequest, user: CurrentUser):
    password = _blank_to_none(body.password)
    with get_db() as conn:
        if not body.roomId and not body.roomKey and not body.roomName:
            raise HTTPException(status_code=400, detail="需要 roomId、roomKey 或 roomName")

        # 显式 roomId/roomKey 总是精确寻址；显式声明 private 时，roomName
        # 首先表示“我自己的命名空间”，不能因为别人的同名房间存在就误加入。
        room = _resolve_room_reference(
            conn,
            room_id=body.roomId,
            room_key=body.roomKey,
            room_name=None
            if (body.roomId or body.roomKey or body.visibility in {"private", "public"})
            else body.roomName,
            user_id=user["id"],
        )
        if (body.roomId is not None or body.roomKey) and room is None:
            raise HTTPException(status_code=404, detail="房间引用不存在")
        room_name = body.roomName or (room["name"] if room else None)
        created = False

        # 仅 roomName 的旧请求：private 按当前调用者的命名空间创建/加入；
        # 若同名 private 已存在于其他命名空间，不能误选。
        if room is None and body.roomName:
            active = _active_rooms_by_name(conn, body.roomName)
            namespace_owner_id = user.get("owner_id") if user["kind"] == "agent" else user["id"]
            if body.visibility == "private":
                room = next(
                    (
                        candidate for candidate in active
                        if candidate["visibility"] == "private"
                        and candidate["namespace_owner_id"] == namespace_owner_id
                    ),
                    None,
                )
            elif body.visibility == "public":
                public = [candidate for candidate in active if candidate["visibility"] == "public"]
                room = public[0] if len(public) == 1 else None
            elif len(active) == 1:
                room = active[0]
            elif len(active) > 1:
                own = [
                    candidate for candidate in active
                    if candidate["visibility"] == "private"
                    and candidate["namespace_owner_id"] == namespace_owner_id
                ]
                if len(own) == 1:
                    room = own[0]
                else:
                    raise _room_reference_error()

        if room and _is_ended(room):
            raise HTTPException(status_code=410, detail="房间已结束")
        if room:
            # 封禁检查在好友/密码检查之前：被 ban 的用户不应得到更具体的访问提示。
            _reject_if_banned(conn, room, user["id"])

        if not room:
            if not room_name:
                raise HTTPException(status_code=404, detail="房间不存在")
            visibility = body.visibility or "private"
            namespace_owner_id = user.get("owner_id") if user["kind"] == "agent" else user["id"]
            room_agent_id = _resolve_room_agent(conn, user, body.roomAgent)
            template = None
            if body.template:
                template = _get_template(conn, body.template)
                if not template:
                    raise HTTPException(status_code=404, detail=f"模板 {body.template} 不存在")
            rules = _blank_to_none(body.rules)
            if rules is None and template:
                rules = template["rules"] or None
            # 场景只在建房时生效；加入已有房间不改动其场景
            if body.scene and body.scene.kind == "library":
                _require_approved_library_model(conn, _library_ref_id(f"{LIBRARY_REF_PREFIX}{body.scene.id or ''}"), user, purpose="scene")
            map3d = _scene_descriptor_json(body.scene) if body.scene else None
            try:
                conn.execute(
                    """
                    INSERT INTO rooms (
                        name, created_by, namespace_owner_id, password_hash, visibility,
                        rules, room_agent_id, template, map3d
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        room_name,
                        user["id"],
                        namespace_owner_id,
                        auth.hash_password(password) if password else None,
                        visibility,
                        rules,
                        room_agent_id,
                        template["name"] if template else None,
                        map3d,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(status_code=409, detail="该命名空间内的房间名已存在") from exc
            room = _get_room(conn, room_name, user_id=user["id"])
            created = True

        member = _member(conn, room["id"], user["id"])
        if not member:
            prior = _member_any(conn, room["id"], user["id"])
            if prior and prior["membership_status"] == "removed":
                raise _friend_error("member_removed", "你已被房主移出该房间", 403)
            if room["visibility"] == "private":
                _require_private_join_access(conn, room, user)
            # 可见性只决定房间是否出现在「公开」列表；加入密码仍然是独立的
            # 访问控制。公开且设了密码的房间，对尚未加入的用户同样必须验密。
            needs_password = (
                room["password_hash"]
                and not _is_room_governor(room, user["id"])
                and not _is_agent_master(user, room)
            )
            if needs_password:
                if not password:
                    raise HTTPException(status_code=403, detail="需要房间密码")
                if not auth.verify_password(password, room["password_hash"]):
                    raise HTTPException(status_code=403, detail="房间密码错误")
            watermark = conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS w FROM messages WHERE room_id = ?",
                (room["id"],),
            ).fetchone()["w"]
            if prior:
                conn.execute(
                    """
                    UPDATE room_members
                    SET membership_status = 'active', joined_at = datetime('now'),
                        last_seen_at = datetime('now'), left_at = NULL, removed_at = NULL,
                        removed_by = NULL, first_visible_msg_id = ?, last_read_msg_id = ?
                    WHERE room_id = ? AND user_id = ?
                    """,
                    (watermark, watermark, room["id"], user["id"]),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO room_members (
                        room_id, user_id, last_seen_at, first_visible_msg_id, last_read_msg_id
                    )
                    VALUES (?, ?, datetime('now'), ?, ?)
                    """,
                    (room["id"], user["id"], watermark, watermark),
                )
        else:
            _touch(conn, room["id"], user["id"])
        # 主动创建/加入 = 明确想看到这个房间，撤销之前的「从我的列表移除」（v2.24）
        conn.execute(
            "DELETE FROM room_hidden WHERE room_id = ? AND user_id = ?",
            (room["id"], user["id"]),
        )
        online = _online_users(conn, room)
        data = {**_room_dict(room, user["id"], online), "created": created, "joined": True}
        data["roomMembers"] = _active_room_members(conn, room)
        _attach_template_fields(conn, room, data)
    return data

@app.get("/api/rooms")
def list_my_rooms(user: CurrentUser):
    with get_db() as conn:
        rows = conn.execute(
            ROOM_LIST_SQL
            + " WHERE r.ended_at IS NULL AND r.archived_at IS NULL AND (r.created_by = ? OR m.user_id IS NOT NULL OR u.owner_id = ?)"
            + " AND NOT EXISTS (SELECT 1 FROM room_hidden h WHERE h.room_id = r.id AND h.user_id = ?)"
            # 「我参与的更新时间」排序（v2.27）：我在房里看到的最新可见消息时间。
            # 没进过 / 没新消息的房保持在旧值，慢慢往下掉；从没进过的按创建时间兜底。
            # 时间戳只到秒，末位再用 id 兜底，保证同一秒内多个房间的顺序稳定。
            + " ORDER BY COALESCE(m.my_update_at, r.created_at) DESC, r.id DESC",
            (ONLINE_WINDOW, user["id"], user["id"], user["id"], user["id"]),
        ).fetchall()
    return {"rooms": [_room_list_dict(row, user["id"]) for row in rows]}


@app.put("/api/rooms/{room_name}/hidden")
def hide_room_from_list(room_name: str, user: CurrentUser):
    """把别人创建的房间从「我的」列表里移除（本人视图过滤，房间与记录原样保留）。

    房主与 Agent 主人无权移除——他们只能「归档房间」；非成员本就看不到该房，
    这里也一并拒绝，避免留下无意义的隐藏行。重新加入房间会自动恢复。
    """
    with get_db() as conn:
        room = _require_active_room(conn, room_name)
        if _can_archive(user, room):
            raise HTTPException(status_code=403, detail="房主不能移除自己的房间，请改用「归档房间」")
        if not _member(conn, room["id"], user["id"]):
            raise HTTPException(status_code=403, detail="只有房间成员可以把该房间从自己的列表移除")
        conn.execute(
            """
            INSERT INTO room_hidden (room_id, user_id, hidden_at)
            VALUES (?, ?, strftime('%Y-%m-%d %H:%M:%f', 'now'))
            ON CONFLICT (room_id, user_id) DO UPDATE SET hidden_at = excluded.hidden_at
            """,
            (room["id"], user["id"]),
        )
    return {"hidden": True, "roomName": room["name"]}


@app.get("/api/rooms/public")
def list_public_rooms(user: CurrentUser):
    with get_db() as conn:
        rows = conn.execute(
            ROOM_LIST_SQL
            + " WHERE r.ended_at IS NULL AND r.archived_at IS NULL AND r.visibility = 'public'"
            + " ORDER BY r.created_at DESC",
            (ONLINE_WINDOW, user["id"]),
        ).fetchall()
    return {"rooms": [_room_list_dict(row, user["id"]) for row in rows]}


@app.get("/api/rooms/{room_name}")
def room_detail(room_name: str, user: CurrentUser):
    with get_db() as conn:
        room, member = _require_membership(conn, room_name, user["id"])
        online = _online_users(conn, room)
        data = _room_dict(room, user["id"], online)
        data["myPermissions"] = {
            "canSpeak": bool(member["can_speak"]),
            "canUpload": bool(member["can_upload"]),
            "canViewHistory": bool(member["can_view_history"]),
            "canEditFiles": bool(member["can_edit_files"]),
        }
        data["memberCount"] = conn.execute(
            "SELECT COUNT(*) AS c FROM room_members WHERE room_id = ? AND membership_status = 'active'", (room["id"],)
        ).fetchone()["c"]
        data["roomMembers"] = _active_room_members(conn, room)
        data["groups"] = _group_dicts(conn, room, user["id"])
        data["files"] = _files_summary(conn, room, member, user["id"])
        _attach_template_fields(conn, room, data)
    return data


@app.patch("/api/rooms/{room_name}")
def update_room(room_name: str, body: RoomUpdate, user: CurrentUser):
    # scene 用 model_fields_set 判定「是否显式传了」：缺席=不改，传了才动（含 kind=none 清空）
    scene_provided = "scene" in body.model_fields_set
    if (
        body.roomName is None
        and body.password is None
        and body.visibility is None
        and body.muted is None
        and body.filesLocked is None
        and body.rules is None
        and body.roomAgent is None
        and not scene_provided
    ):
        raise HTTPException(status_code=400, detail="没有需要修改的字段")
    with get_db() as conn:
        room = _require_active_room(conn, room_name)
        _require_owner(room, user["id"])
        if (
            _row_get(room, "room_type", "standard") == "direct_pair"
            and body.visibility == "public"
        ):
            raise _friend_error("pair_room_only", "双人好友房不能改为公开房间", 403)
        new_name = body.roomName.strip() if body.roomName else room["name"]
        if body.password is None:
            password_hash = room["password_hash"]
        else:
            password = _blank_to_none(body.password)
            password_hash = auth.hash_password(password) if password else None
        visibility = body.visibility or room["visibility"]
        if new_name.lower() != room["name"].lower() or visibility != room["visibility"]:
            if visibility == "public":
                conflict = conn.execute(
                    """
                    SELECT 1 FROM rooms
                    WHERE name = ? COLLATE NOCASE AND id != ?
                      AND visibility = 'public' AND archived_at IS NULL
                    LIMIT 1
                    """,
                    (new_name, room["id"]),
                ).fetchone()
            else:
                conflict = conn.execute(
                    """
                    SELECT 1 FROM rooms
                    WHERE name = ? COLLATE NOCASE AND id != ?
                      AND visibility = 'private'
                      AND namespace_owner_id = ? AND archived_at IS NULL
                    LIMIT 1
                    """,
                    (new_name, room["id"], _room_namespace_owner_id(room)),
                ).fetchone()
            if conflict:
                raise HTTPException(status_code=409, detail="目标命名空间内的房间名已存在")
        muted = room["muted"] if body.muted is None else int(body.muted)
        files_locked = room["files_locked"] if body.filesLocked is None else int(body.filesLocked)
        rules = room["rules"] if body.rules is None else (_blank_to_none(body.rules) or "")
        if body.roomAgent is None:
            room_agent_id = room["room_agent_id"]
        else:
            room_agent_id = _resolve_room_agent(conn, user, body.roomAgent)
        conn.execute(
            """
            UPDATE rooms
            SET name = ?, password_hash = ?, visibility = ?, muted = ?, files_locked = ?,
                rules = ?, room_agent_id = ?
            WHERE id = ?
            """,
            (new_name, password_hash, visibility, muted, files_locked, rules, room_agent_id, room["id"]),
        )
        if files_locked != room["files_locked"]:
            # 锁定状态是文件列表元信息的一部分，递增 revision 让文件长轮询者也醒来
            conn.execute(
                "UPDATE rooms SET files_revision = files_revision + 1 WHERE id = ?", (room["id"],)
            )
        if scene_provided:
            # 资源库场景须为本人已过审模型；换成内置/外链/资源库/清空时一并
            # 丢弃已上传的场景本体（换回 file 需重新上传）
            if body.scene and body.scene.kind == "library":
                _require_approved_library_model(conn, _library_ref_id(f"{LIBRARY_REF_PREFIX}{body.scene.id or ''}"), user, purpose="scene")
            conn.execute(
                """
                UPDATE rooms
                SET map3d = ?, scene_data = NULL, scene_mime = NULL, scene_updated_at = NULL
                WHERE id = ?
                """,
                (_scene_descriptor_json(body.scene) if body.scene else None, room["id"]),
            )
        _touch(conn, room["id"], user["id"])
        room = _get_room_by_id(conn, room["id"])
        online = _online_users(conn, room)
    if scene_provided:
        notify_room(room["id"])  # 唤醒长轮询，让房内其他人立刻看到场景变化
    return _room_dict(room, user["id"], online)


@app.get("/api/room-scenes")
def list_room_scenes(user: CurrentUser):
    """内置 3D 场景目录（房间可选的标准场景）。几何由渲染端按 id 搭建，这里只给清单。"""
    return {"scenes": [dict(scene) for scene in BUILTIN_ROOM_SCENES]}


@app.get("/api/avatar-models")
def list_avatar_models():
    """内置缺省 3D 形象目录。账号用 `builtin:<id>` 引用（PUT /api/me/model3d），
    模型本体是静态资源（static/avatars/），这里给清单、缩略图与能力位。

    **公开接口（不要加回 CurrentUser）**：注册弹窗在未登录状态就要展示内置形象
    供新账号挑选（「3D 形象 → 设置」打开的形象弹窗读的就是这里）。它只返回模块
    常量 BUILTIN_AVATARS，不含任何用户数据、不查库；模型与缩略图本来就在
    /static/avatars/ 下免登录可取，加 token 只会让新访客看到空列表。"""
    return {
        "avatars": [
            {
                "id": avatar["id"],
                "name": avatar["name"],
                "nameEn": avatar["nameEn"],
                "description": avatar["description"],
                "descriptionEn": avatar["descriptionEn"],
                "file": avatar["file"],
                "thumbnail": avatar["thumbnail"],
                "arkit": avatar["arkit"],
                "humanoid": avatar["humanoid"],
                "source": avatar["source"],
            }
            for avatar in BUILTIN_AVATARS
        ]
    }


@app.post("/api/rooms/{room_name}/scene")
async def upload_room_scene(room_name: str, user: CurrentUser, file: UploadFile = File(...)):
    """上传房间 3D 场景（GLB，≤50MB）。上传即生效，无需再 PATCH。"""
    data = await file.read()
    mime = _validate_room_scene_bytes(data)
    safe_name = _sanitize_filename(file.filename)
    with get_db() as conn:
        room = _require_active_room(conn, room_name)
        _require_owner(room, user["id"])
        now = _db_now(conn)
        # 描述符只记 kind；下载地址由 _room_scene_descriptor 按房间名与版本号现算，
        # 这样房间改名不会让已存的场景地址失效。
        conn.execute(
            """
            UPDATE rooms
            SET scene_data = ?, scene_mime = ?, scene_updated_at = ?, map3d = ?
            WHERE id = ?
            """,
            (data, mime, now, json.dumps({"kind": "file"}, ensure_ascii=False), room["id"]),
        )
        room_id = room["id"]
        room = _get_room_by_id(conn, room_id)
        online = _online_users(conn, room)
    notify_room(room_id)
    return {**_room_dict(room, user["id"], online), "sceneFilename": safe_name}


@app.get("/api/rooms/{room_name}/scene")
def get_room_scene(room_name: str, user: CurrentUser):
    """下载房间已上传的场景文件（成员可读）。非上传类场景一律 404。"""
    with get_db() as conn:
        room, _ = _require_membership(conn, room_name, user["id"])
        desc = _room_scene_descriptor(room)
        if not desc or desc["kind"] != "file":
            raise HTTPException(status_code=404, detail="该房间没有上传的场景文件")
        row = conn.execute(
            "SELECT scene_data, scene_mime FROM rooms WHERE id = ?", (room["id"],)
        ).fetchone()
        if not row or not row["scene_data"]:
            raise HTTPException(status_code=404, detail="该房间没有上传的场景文件")
        data = row["scene_data"]
        mime = row["scene_mime"] or "model/gltf-binary"
    return Response(
        content=data,
        media_type=mime,
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@app.delete("/api/rooms/{room_name}/scene")
def clear_room_scene(room_name: str, user: CurrentUser):
    """清除房间场景（回到展厅环境）。"""
    with get_db() as conn:
        room = _require_active_room(conn, room_name)
        _require_owner(room, user["id"])
        conn.execute(
            """
            UPDATE rooms
            SET map3d = NULL, scene_data = NULL, scene_mime = NULL, scene_updated_at = NULL
            WHERE id = ?
            """,
            (room["id"],),
        )
        room_id = room["id"]
        room = _get_room(conn, room_name)
        online = _online_users(conn, room)
    notify_room(room_id)
    return _room_dict(room, user["id"], online)


@app.post("/api/rooms/{room_name}/archive")
def archive_room(room_name: str, user: CurrentUser):
    with get_db() as conn:
        room = _require_active_room(conn, room_name)
        if not _can_archive(user, room):
            raise HTTPException(status_code=403, detail="只有房主或 Agent 主人可以归档该房间")
        conn.execute(
            "UPDATE rooms SET archived_at = datetime('now'), ended_at = datetime('now') WHERE id = ?",
            (room["id"],),
        )
        conn.execute(
            """
            UPDATE messages
            SET streaming = 0, updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now')
            WHERE room_id = ? AND streaming = 1
            """,
            (room["id"],),
        )
        room = _get_room_by_id(conn, room["id"])
    return {**_room_dict(room, user["id"]), "archived": True}


@app.delete("/api/rooms/{room_name}")
def end_room(room_name: str, user: CurrentUser):
    return archive_room(room_name, user)


@app.get("/api/archives")
def list_archives(user: CurrentUser):
    with get_db() as conn:
        rows = conn.execute(
            ROOM_LIST_SQL
            + " WHERE r.archived_at IS NOT NULL AND (r.created_by = ? OR u.owner_id = ? OR EXISTS ("
            + "SELECT 1 FROM room_members ma WHERE ma.room_id = r.id AND ma.user_id = ? AND ma.membership_status != 'removed'))"
            + " ORDER BY r.archived_at DESC",
            (ONLINE_WINDOW, user["id"], user["id"], user["id"], user["id"]),
        ).fetchall()
    return {"rooms": [_room_list_dict(row, user["id"]) for row in rows]}


@app.get("/api/archives/{room_id}")
def archive_detail(room_id: int, user: CurrentUser):
    with get_db() as conn:
        room, member = _require_archive_access(conn, room_id, user)
        data = _room_dict(room, user["id"], [])
        data["readOnly"] = True
        data["memberCount"] = conn.execute(
            "SELECT COUNT(*) AS c FROM room_members WHERE room_id = ? AND membership_status = 'active'", (room["id"],)
        ).fetchone()["c"]
        if member:
            data["myPermissions"] = {
                "canSpeak": False,
                "canUpload": False,
                "canViewHistory": bool(member["can_view_history"]),
                "canEditFiles": False,
            }
        # 归档只读：canEdit 恒 False；文件列表与内容经归档接口只读访问
        data["files"] = {
            "revision": room["files_revision"],
            "locked": bool(room["files_locked"]),
            "canEdit": False,
            "count": conn.execute(
                "SELECT COUNT(*) AS c FROM room_files WHERE room_id = ?", (room["id"],)
            ).fetchone()["c"],
        }
    return data


@app.get("/api/archives/{room_id}/messages")
def archive_messages(
    room_id: int,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
    after_id: Annotated[int | None, Query(alias="afterId", ge=0)] = None,
):
    with get_db() as conn:
        room, member = _require_archive_access(conn, room_id, user)
        rows = _fetch_messages(
            conn, room, member, user["id"], limit, after_id, skip_history=_can_archive(user, room)
        )
        items = _message_dicts(conn, rows, room["name"], room=room, user_id=user["id"], archive_id=room["id"])
    return {
        "roomId": room["id"],
        "roomName": room["name"],
        "messages": items,
    }


@app.get("/api/archives/{room_id}/attachments/{message_id}")
def download_archived_attachment(room_id: int, message_id: int, user: CurrentUser):
    with get_db() as conn:
        room, _member = _require_archive_access(conn, room_id, user)
        row = conn.execute(
            """
            SELECT m.attachment_name, m.attachment_path, m.msg_type, m.user_id,
                   m.whisper_to, m.whisper_to_ids
            FROM messages m
            WHERE m.id = ? AND m.room_id = ? AND m.msg_type IN ('attachment', 'image', 'voice')
            """,
            (message_id, room_id),
        ).fetchone()
        _require_file_visible(row, room, user["id"])
        if not row["attachment_path"]:
            raise HTTPException(status_code=404, detail="附件不存在")
        path = UPLOADS_DIR / row["attachment_path"]
        if not path.is_file():
            raise HTTPException(status_code=404, detail="附件文件缺失")
        media = _voice_media_type(row["attachment_name"]) if row["msg_type"] == "voice" else None
    return _file_response(path, row["attachment_name"], inline=row["msg_type"] in ("image", "voice"), media_type=media)


# ---------- 成员与权限 ----------

def _member_dict(row, online_cutoff: str = ONLINE_WINDOW) -> dict:
    return {
        "userId": row["user_id"],
        "username": row["username"],
        "kind": row["kind"],
        "joinedAt": row["joined_at"],
        "membershipStatus": _row_get(row, "membership_status", "active"),
        "inRoom": bool(row["online"]),
        "canSpeak": bool(row["can_speak"]),
        "canUpload": bool(row["can_upload"]),
        "canViewHistory": bool(row["can_view_history"]),
        "canEditFiles": bool(row["can_edit_files"]),
        "online": bool(row["online"]),
        "avatarUrl": _avatar_url(row["username"], _row_get(row, "avatarV")),
        # v2.23 封禁状态（仅 /members 查询带出；缺列时按未封禁处理）
        "banned": bool(_row_get(row, "banned", 0)),
        "banExpiresAt": _row_get(row, "ban_expires"),
    }


@app.get("/api/rooms/{room_name}/members")
def list_members(room_name: str, user: CurrentUser):
    with get_db() as conn:
        room, _member = _require_membership(conn, room_name, user["id"])
        rows = conn.execute(
            """
            SELECT m.*, u.username, u.kind, u.avatar_updated_at AS avatarV,
                   CASE WHEN m.last_seen_at > datetime('now', ?)
                        AND (b.id IS NULL OR (b.expires_at IS NOT NULL AND b.expires_at <= strftime('%Y-%m-%d %H:%M:%f', 'now')))
                       THEN 1 ELSE 0 END AS online,
                   CASE WHEN b.id IS NOT NULL AND (b.expires_at IS NULL
                        OR b.expires_at > strftime('%Y-%m-%d %H:%M:%f', 'now'))
                       THEN 1 ELSE 0 END AS banned,
                   b.expires_at AS ban_expires
            FROM room_members m
            JOIN users u ON u.id = m.user_id
            LEFT JOIN room_bans b ON b.room_id = m.room_id AND b.user_id = m.user_id
            WHERE m.room_id = ? AND m.membership_status = 'active'
            ORDER BY m.joined_at ASC
            """,
            (ONLINE_WINDOW, room["id"]),
        ).fetchall()
        members = []
        for row in rows:
            item = _member_dict(row)
            item["isOwner"] = row["user_id"] == room["created_by"]
            item["isRoomAgent"] = row["user_id"] == room["room_agent_id"]
            item["isAgentMaster"] = (
                _row_get(room, "creatorKind") == "agent"
                and _row_get(room, "creatorOwnerId") == row["user_id"]
            )
            members.append(item)
    return {"roomName": room["name"], "members": members}


@app.post("/api/rooms/{room_name}/leave")
def leave_room(room_name: str, user: CurrentUser):
    with get_db() as conn:
        room, _member = _require_membership(conn, room_name, user["id"])
        conn.execute(
            """
            UPDATE room_members
            SET membership_status = 'left', left_at = datetime('now'), last_seen_at = datetime('now')
            WHERE room_id = ? AND user_id = ? AND membership_status = 'active'
            """,
            (room["id"], user["id"]),
        )
        now = _db_now(conn)
        conn.execute("DELETE FROM room_presence WHERE room_id = ? AND user_id = ?", (room["id"], user["id"]))
        conn.execute(
            "INSERT INTO room_presence_log (room_id, user_id, kind, created_at) VALUES (?, ?, 'leave', ?)",
            (room["id"], user["id"], now),
        )
        room_id = room["id"]
    notify_room(room_id)
    return {"left": True, "roomId": room_id}


@app.delete("/api/rooms/{room_name}/members/{username}")
def remove_room_member(room_name: str, username: str, user: CurrentUser):
    with get_db() as conn:
        room = _require_active_room(conn, room_name, user_id=user["id"])
        _require_owner(room, user["id"])
        target = _get_user(conn, username)
        if not target:
            raise HTTPException(status_code=404, detail="用户不存在")
        member = _member(conn, room["id"], target["id"])
        if not member:
            raise HTTPException(status_code=404, detail="该用户不是活动房间成员")
        if _is_room_governor(room, target["id"]):
            raise HTTPException(status_code=403, detail="不能移除房间治理者")
        conn.execute(
            """
            UPDATE room_members
            SET membership_status = 'removed', removed_at = datetime('now'), removed_by = ?,
                last_seen_at = datetime('now')
            WHERE room_id = ? AND user_id = ?
            """,
            (user["id"], room["id"], target["id"]),
        )
        now = _db_now(conn)
        conn.execute("DELETE FROM room_presence WHERE room_id = ? AND user_id = ?", (room["id"], target["id"]))
        conn.execute(
            "INSERT INTO room_presence_log (room_id, user_id, kind, created_at) VALUES (?, ?, 'leave', ?)",
            (room["id"], target["id"], now),
        )
        room_id = room["id"]
    notify_room(room_id)
    return {"removed": True, "username": target["username"], "roomId": room_id}


@app.put("/api/rooms/{room_name}/permissions/{username}")
def set_permissions(room_name: str, username: str, body: PermissionUpdate, user: CurrentUser):
    if (
        body.canSpeak is None
        and body.canUpload is None
        and body.canViewHistory is None
        and body.canEditFiles is None
    ):
        raise HTTPException(status_code=400, detail="没有需要修改的权限")
    with get_db() as conn:
        room = _require_active_room(conn, room_name)
        _require_owner(room, user["id"])
        target = _get_user(conn, username)
        if not target:
            raise HTTPException(status_code=404, detail="用户不存在")
        if _is_room_governor(room, target["id"]):
            raise HTTPException(status_code=403, detail="房主、房间管理 Agent 与建房的 Agent 主人不可被限制")
        member = _member(conn, room["id"], target["id"])
        if not member:
            raise HTTPException(status_code=404, detail="该用户尚未加入房间")
        conn.execute(
            """
            UPDATE room_members
            SET can_speak = ?, can_upload = ?, can_view_history = ?, can_edit_files = ?
            WHERE room_id = ? AND user_id = ?
            """,
            (
                int(member["can_speak"] if body.canSpeak is None else body.canSpeak),
                int(member["can_upload"] if body.canUpload is None else body.canUpload),
                int(member["can_view_history"] if body.canViewHistory is None else body.canViewHistory),
                int(member["can_edit_files"] if body.canEditFiles is None else body.canEditFiles),
                room["id"],
                target["id"],
            ),
        )
        member = _member(conn, room["id"], target["id"])
    return {
        "roomName": room["name"],
        "username": target["username"],
        "canSpeak": bool(member["can_speak"]),
        "canUpload": bool(member["can_upload"]),
        "canViewHistory": bool(member["can_view_history"]),
        "canEditFiles": bool(member["can_edit_files"]),
    }


# ---------- 房间封禁（v2.23）：管理员把用户 ban 出房间 ----------

@app.get("/api/rooms/{room_name}/bans")
def list_bans(room_name: str, user: CurrentUser):
    """封禁名单（含已过期的记录行，标 active=false），仅房主/roomAgent 可见。"""
    with get_db() as conn:
        room = _require_active_room(conn, room_name)
        _require_owner(room, user["id"])
        now = _db_now(conn)
        rows = conn.execute(
            """
            SELECT b.expires_at, b.created_at, u.username, u.kind,
                   ban_by.username AS banned_by_name
            FROM room_bans b
            JOIN users u ON u.id = b.user_id
            JOIN users ban_by ON ban_by.id = b.banned_by
            WHERE b.room_id = ?
            ORDER BY b.created_at DESC
            """,
            (room["id"],),
        ).fetchall()
    return {
        "roomName": room["name"],
        "bans": [
            {
                "username": row["username"],
                "kind": row["kind"],
                "bannedBy": row["banned_by_name"],
                "bannedAt": row["created_at"],
                "expiresAt": row["expires_at"],
                "active": row["expires_at"] is None or row["expires_at"] > now,
            }
            for row in rows
        ],
    }


@app.post("/api/rooms/{room_name}/bans")
def ban_member(room_name: str, body: BanCreate, user: CurrentUser):
    """封禁用户：封禁期间无法加入房间、无法读取任何房间数据（消息/文件/在线
    列表等全部 403），被封禁时在线的长轮询立即被唤醒踢出。重复封禁覆盖时长。
    目标不必是成员（可预先 ban 掉捣乱者），但房主与 roomAgent 不可被封禁。"""
    with get_db() as conn:
        room = _require_active_room(conn, room_name)
        _require_owner(room, user["id"])
        target = _get_user(conn, body.username)
        if not target:
            raise HTTPException(status_code=404, detail="用户不存在")
        if _is_room_governor(room, target["id"]):
            raise HTTPException(status_code=403, detail="房主、房间管理 Agent 与建房的 Agent 主人不可被封禁")
        modifier = BAN_DURATION_MODIFIERS[body.duration]
        expires = (
            None
            if modifier is None
            else conn.execute(
                "SELECT strftime('%Y-%m-%d %H:%M:%f', 'now', ?) AS t", (modifier,)
            ).fetchone()["t"]
        )
        conn.execute(
            """
            INSERT INTO room_bans (room_id, user_id, banned_by, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (room_id, user_id) DO UPDATE SET
                banned_by = excluded.banned_by,
                created_at = strftime('%Y-%m-%d %H:%M:%f', 'now'),
                expires_at = excluded.expires_at
            """,
            (room["id"], target["id"], user["id"], expires),
        )
        room_id = room["id"]
    notify_room(room_id)
    return {
        "roomName": room["name"],
        "username": target["username"],
        "duration": body.duration,
        "expiresAt": expires,
    }


@app.delete("/api/rooms/{room_name}/bans/{username}")
def unban_member(room_name: str, username: str, user: CurrentUser):
    """解除封禁（删除封禁行，含已过期的记录行）。"""
    with get_db() as conn:
        room = _require_active_room(conn, room_name)
        _require_owner(room, user["id"])
        target = _get_user(conn, username)
        if not target:
            raise HTTPException(status_code=404, detail="用户不存在")
        cur = conn.execute(
            "DELETE FROM room_bans WHERE room_id = ? AND user_id = ?",
            (room["id"], target["id"]),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="该用户未被封禁")
    return {"roomName": room["name"], "username": target["username"], "unbanned": True}


# ---------- 私聊权限（白名单 / 黑名单，仅房主管理） ----------

WHISPER_RULE_SELECT = """
            SELECT id, room_id, list_type, priority, sender, receiver, created_at AS createdAt
            FROM whisper_rules
"""


def _whisper_rule_dict(row) -> dict:
    return {
        "id": row["id"],
        "listType": row["list_type"],
        "priority": row["priority"],
        "sender": row["sender"],
        "receiver": row["receiver"],
        "createdAt": row["createdAt"],
    }


def _require_room_owner_conn(conn, room_name: str, user: dict):
    room = _require_active_room(conn, room_name)
    _require_owner(room, user["id"])
    return room


@app.get("/api/rooms/{room_name}/whisper-rules")
def list_whisper_rules(room_name: str, user: CurrentUser):
    with get_db() as conn:
        room = _require_room_owner_conn(conn, room_name, user)
        rows = conn.execute(
            WHISPER_RULE_SELECT + " WHERE room_id = ? ORDER BY priority DESC, id ASC",
            (room["id"],),
        ).fetchall()
    return {"roomName": room["name"], "rules": [_whisper_rule_dict(row) for row in rows]}


@app.post("/api/rooms/{room_name}/whisper-rules")
def add_whisper_rule(room_name: str, body: WhisperRuleCreate, user: CurrentUser):
    def _canonical(name: str) -> str:
        if name == "*":
            return "*"
        u = _get_user(conn, name)
        if not u:
            raise HTTPException(status_code=404, detail=f"用户 {name} 不存在")
        return u["username"]

    with get_db() as conn:
        room = _require_room_owner_conn(conn, room_name, user)
        try:
            cur = conn.execute(
                """
                INSERT INTO whisper_rules (room_id, list_type, priority, sender, receiver)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    room["id"],
                    body.listType,
                    body.priority,
                    _canonical(body.sender),
                    _canonical(body.receiver),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="相同的规则已存在") from exc
        row = conn.execute(WHISPER_RULE_SELECT + " WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _whisper_rule_dict(row)


@app.delete("/api/rooms/{room_name}/whisper-rules/{rule_id}")
def delete_whisper_rule(room_name: str, rule_id: int, user: CurrentUser):
    with get_db() as conn:
        room = _require_room_owner_conn(conn, room_name, user)
        cur = conn.execute(
            "DELETE FROM whisper_rules WHERE id = ? AND room_id = ?",
            (rule_id, room["id"]),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="规则不存在")
    return {"deleted": True, "id": rule_id}


# ---------- 房间群组（v2.8） ----------

@app.get("/api/rooms/{room_name}/groups")
def list_groups(room_name: str, user: CurrentUser):
    with get_db() as conn:
        room, _member = _require_membership(conn, room_name, user["id"])
        return {"roomName": room["name"], "groups": _group_dicts(conn, room, user["id"])}


@app.post("/api/rooms/{room_name}/groups")
def create_group(room_name: str, body: GroupCreate, user: CurrentUser):
    """创建命名群组（仅房主/roomAgent）。成员名单即白名单，发 `#群名` 展开到全组。"""
    with get_db() as conn:
        room = _require_active_room(conn, room_name)
        _require_owner(room, user["id"])
        if _get_group(conn, room["id"], body.name):
            raise HTTPException(status_code=409, detail="同名群组已存在")
        ids = _validate_group_members(conn, room["id"], body.members)
        conn.execute(
            "INSERT INTO room_groups (room_id, name, created_by) VALUES (?, ?, ?)",
            (room["id"], body.name, user["id"]),
        )
        gid = conn.execute("SELECT last_insert_rowid() AS gid").fetchone()["gid"]
        conn.executemany(
            "INSERT INTO room_group_members (group_id, user_id) VALUES (?, ?)",
            [(gid, i) for i in ids],
        )
        members = [m["username"] for m in _group_members(conn, gid)]
        name = body.name
    return {"name": name, "members": members}


@app.patch("/api/rooms/{room_name}/groups/{group_name}")
def update_group(room_name: str, group_name: str, body: GroupUpdate, user: CurrentUser):
    """整体替换群组成员名单（仅房主/roomAgent）。"""
    with get_db() as conn:
        room = _require_active_room(conn, room_name)
        _require_owner(room, user["id"])
        group = _get_group(conn, room["id"], group_name)
        if not group:
            raise HTTPException(status_code=404, detail="群组不存在")
        ids = _validate_group_members(conn, room["id"], body.members)
        conn.execute("DELETE FROM room_group_members WHERE group_id = ?", (group["id"],))
        conn.executemany(
            "INSERT INTO room_group_members (group_id, user_id) VALUES (?, ?)",
            [(group["id"], i) for i in ids],
        )
        members = [m["username"] for m in _group_members(conn, group["id"])]
        name = group["name"]
    return {"name": name, "members": members}


@app.delete("/api/rooms/{room_name}/groups/{group_name}")
def delete_group(room_name: str, group_name: str, user: CurrentUser):
    with get_db() as conn:
        room = _require_active_room(conn, room_name)
        _require_owner(room, user["id"])
        cur = conn.execute(
            "DELETE FROM room_groups WHERE room_id = ? AND name = ?",
            (room["id"], group_name),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="群组不存在")
    return {"deleted": True, "name": group_name}


# ---------- 房间模板 ----------

MAX_SCRIPT_BYTES = 5 * 1024 * 1024


def _get_template(conn, name: str):
    return conn.execute(
        "SELECT * FROM room_templates WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()


def _template_dict(row) -> dict:
    return {
        "name": row["name"],
        "title": row["title"],
        "description": row["description"],
        "rules": _fill_base_url(row["rules"]),
        "params": _json_loads(row["params"]),
        "scriptName": row["script_name"],
        "scriptSize": len(row["script_data"]) if row["script_data"] else 0,
        "createdBy": row["created_by"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _json_loads(raw: str | None) -> dict:
    import json
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def _decode_script(script_name: str | None, script_base64: str | None) -> bytes | None:
    """校验并解码脚本附件；两者必须成对出现，解码后 ≤5MB。"""
    if script_base64 is None:
        if script_name is not None:
            raise HTTPException(status_code=400, detail="提供 scriptName 时必须同时提供 scriptBase64")
        return None
    if not script_name:
        raise HTTPException(status_code=400, detail="提供 scriptBase64 时必须同时提供 scriptName")
    try:
        data = base64.b64decode(script_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="scriptBase64 不是合法的 base64") from exc
    if not data:
        raise HTTPException(status_code=400, detail="脚本附件不能为空")
    if len(data) > MAX_SCRIPT_BYTES:
        raise HTTPException(status_code=400, detail=f"脚本附件不能超过 {MAX_SCRIPT_BYTES // (1024 * 1024)}MB")
    return data


def _attach_template_fields(conn, room, data: dict) -> dict:
    """房间详情补充模板脚本信息，供 Room Agent 下载脚本（模板已删则只留来源名）。"""
    name = _row_get(room, "template")
    if not name:
        return data
    tpl = _get_template(conn, name)
    data["templateTitle"] = tpl["title"] if tpl else None
    data["templateScript"] = tpl["script_name"] if tpl else None
    return data


@app.get("/api/room-templates")
def list_room_templates(user: CurrentUser):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM room_templates ORDER BY (created_by IS NULL) DESC, id ASC"
        ).fetchall()
    return {"templates": [_template_dict(r) for r in rows]}


@app.post("/api/room-templates")
def create_room_template(body: TemplateCreate, user: CurrentUser):
    script_data = _decode_script(body.scriptName, body.scriptBase64)
    import json
    params_json = json.dumps(body.params, ensure_ascii=False)
    with get_db() as conn:
        try:
            conn.execute(
                """
                INSERT INTO room_templates (name, title, description, rules, params, script_name, script_data, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    body.name, body.title, body.description, body.rules,
                    params_json, body.scriptName, script_data, user["id"],
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="模板名已存在") from exc
        row = _get_template(conn, body.name)
    return _template_dict(row)


@app.get("/api/room-templates/{name}")
def get_room_template(name: str, user: CurrentUser):
    with get_db() as conn:
        row = _get_template(conn, name)
    if not row:
        raise HTTPException(status_code=404, detail="模板不存在")
    return _template_dict(row)


@app.patch("/api/room-templates/{name}")
def update_room_template(name: str, body: TemplateUpdate, user: CurrentUser):
    with get_db() as conn:
        row = _get_template(conn, name)
        if not row:
            raise HTTPException(status_code=404, detail="模板不存在")
        if row["created_by"] is None:
            raise HTTPException(status_code=403, detail="系统内置模板不可修改")
        if row["created_by"] != user["id"]:
            raise HTTPException(status_code=403, detail="只有模板发布者可以修改")
        title = row["title"] if body.title is None else body.title
        description = row["description"] if body.description is None else body.description
        rules = row["rules"] if body.rules is None else body.rules
        import json
        params_json = row["params"] if body.params is None else json.dumps(body.params, ensure_ascii=False)
        script_data = row["script_data"]
        script_name = row["script_name"]
        if body.scriptBase64 is not None:
            script_data = _decode_script(body.scriptName, body.scriptBase64)
            script_name = body.scriptName
        elif body.scriptName is not None:
            script_name = body.scriptName  # 只改附件名，内容保留
        conn.execute(
            """
            UPDATE room_templates
            SET title = ?, description = ?, rules = ?, params = ?, script_name = ?, script_data = ?,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (title, description, rules, params_json, script_name, script_data, row["id"]),
        )
        row = _get_template(conn, name)
    return _template_dict(row)


@app.delete("/api/room-templates/{name}")
def delete_room_template(name: str, user: CurrentUser):
    with get_db() as conn:
        row = _get_template(conn, name)
        if not row:
            raise HTTPException(status_code=404, detail="模板不存在")
        if row["created_by"] is None:
            raise HTTPException(status_code=403, detail="系统内置模板不可删除")
        if row["created_by"] != user["id"]:
            raise HTTPException(status_code=403, detail="只有模板发布者可以删除")
        conn.execute("DELETE FROM room_templates WHERE id = ?", (row["id"],))
    return {"deleted": True, "name": name}


def _template_script_response(row) -> Response:
    script_name = row["script_name"] or "script.bin"
    return Response(
        row["script_data"],
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename=\"{quote(script_name)}\"; filename*=UTF-8''{quote(script_name)}",
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/room-templates/{name}/script")
def download_room_template_script(name: str, user: CurrentUser):
    with get_db() as conn:
        row = _get_template(conn, name)
    if not row or not row["script_data"]:
        raise HTTPException(status_code=404, detail="该模板没有脚本附件")
    return _template_script_response(row)


@app.get("/scripts/templates/{name}")
def download_template_script_static(name: str):
    """模板脚本免登录静态下载；房间 rules 里写的下载地址就是它。"""
    with get_db() as conn:
        row = _get_template(conn, name)
    if not row or not row["script_data"]:
        raise HTTPException(status_code=404, detail="该模板没有脚本附件")
    return _template_script_response(row)


# ---------- 消息与附件 ----------

MESSAGE_SELECT = """
            SELECT m.id, m.content, m.msg_type, m.attachment_name, m.created_at AS createdAt,
                   COALESCE(m.streaming, 0) AS streaming,
                   COALESCE(m.updated_at, m.created_at) AS updatedAt,
                   m.user_id, m.whisper_to, m.whisper_to_ids, m.mention_to, m.reply_to,
                   COALESCE(m.recalled, 0) AS recalled, m.duration_ms,
                   u.username, u.avatar_updated_at AS avatarV,
                   mu.username AS mentionUsername, mu.avatar_updated_at AS mentionAvatarV,
                   rp.user_id AS replyUserId, rp.content AS replyContent,
                   rp.msg_type AS replyType, rp.attachment_name AS replyAttachment,
                   COALESCE(rp.recalled, 0) AS replyRecalled,
                   rp.whisper_to AS replyWhisperTo, rp.whisper_to_ids AS replyWhisperIds,
                   ru.username AS replyUsername
            FROM messages m JOIN users u ON u.id = m.user_id
            LEFT JOIN users mu ON mu.id = m.mention_to
            LEFT JOIN messages rp ON rp.id = m.reply_to
            LEFT JOIN users ru ON ru.id = rp.user_id
"""


def _db_now(conn) -> str:
    return conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now') AS t").fetchone()["t"]


def _load_message(conn, message_id: int):
    return conn.execute(MESSAGE_SELECT + " WHERE m.id = ?", (message_id,)).fetchone()


def _parse_stream_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    ids: list[int] = []
    for part in raw.split(",")[:MAX_STREAM_IDS]:
        part = part.strip()
        if part.isdigit():
            n = int(part)
            if n > 0:
                ids.append(n)
    return ids


def _finalize_stale_streams(conn, room_id: int) -> None:
    conn.execute(
        """
        UPDATE messages
        SET streaming = 0, updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now')
        WHERE room_id = ? AND streaming = 1
          AND updated_at < strftime('%Y-%m-%d %H:%M:%f', 'now', '-2 minutes')
        """,
        (room_id,),
    )


def _whisper_visible(row, room, user_id: int) -> bool:
    """私聊可见性判据：发送者、全部接收者、房主与 roomAgent 可见，其余不可见。

    公开发言（无接收者）对全房可见。消息渲染（_visible_rows）与「我参与的
    更新时间」（_newest_visible_at）共用这一处，避免两处判据各自漂移。
    """
    recipients = _whisper_ids(row)
    return (
        not recipients
        or user_id in (row["user_id"], *recipients)
        or _is_room_governor(room, user_id)
    )


def _visible_rows(rows, room, user_id: int) -> list[dict]:
    """私聊可见性：只有发送者、全部接收者、房主与 roomAgent 能看到内容。

    其余请求者拿到的行被抹成“空行”——保留 id 让增量游标（afterId）不乱，
    但不泄露发送者、内容与引用目标；UI 端忽略空行不渲染。

    抹掉的行会打上内标 `_blanked`：抹掉后 `_whisper_ids` 已空、光看内容判不出
    可见性，`_newest_visible_at` 靠这个标记把它排除在「我看到的新消息」之外。
    `_blanked` 只在内部流转，`_message_dict` 逐字段组装响应，不会外泄。
    """
    result = []
    for row in rows:
        item = dict(row)
        if not _whisper_visible(item, room, user_id):
            item["_blanked"] = True
            item["content"] = ""
            item["username"] = ""
            item["avatarV"] = None
            item["msg_type"] = "text"
            item["streaming"] = 0
            item["attachment_name"] = None
            item["attachment_path"] = None
            item["whisper_to"] = None
            item["whisper_to_ids"] = None
            item["mention_to"] = None
            item["reply_to"] = None
        result.append(item)
    return result


def _newest_visible_at(room, user_id: int, rows) -> str | None:
    """rows 里此刻我能看到的最新一条消息的 created_at（可见性同 _whisper_visible）。

    没有可见消息时返回 None——别人的私聊对我不可见，不构成「我看到的新消息」。
    """
    best = None
    for row in rows:
        if _row_get(row, "_blanked") or not _whisper_visible(row, room, user_id):
            continue
        if best is None or row["id"] > best["id"]:
            best = row
    if best is None:
        return None
    return _row_get(best, "createdAt") or _row_get(best, "created_at")


def _bump_my_update(conn, room, user_id: int, created_at: str | None) -> None:
    """把「我参与的更新时间」推进到我此刻能看到的最新消息时间（只增不减）。

    没进过房、或进去时没人说话，值就不动，房间在列表里自然往下掉。老库的
    存量成员没有该值（NULL），排序时回落到房间创建时间，不会突然乱序。
    """
    if not created_at:
        return
    conn.execute(
        """
        UPDATE room_members SET my_update_at = MAX(COALESCE(my_update_at, ''), ?)
        WHERE room_id = ? AND user_id = ?
        """,
        (created_at, room["id"], user_id),
    )


def _require_file_visible(row, room, user_id: int) -> None:
    """附件/语音下载前的私聊可见性校验；消息不存在 → 404。"""
    if row is None:
        raise HTTPException(status_code=404, detail="附件不存在")
    recipients = _whisper_ids(row)
    if recipients and user_id not in (row["user_id"], *recipients) and not _is_room_governor(room, user_id):
        raise HTTPException(status_code=403, detail="该附件属于私聊消息，对你不可见")


def _fetch_messages(
    conn,
    room,
    member,
    user_id: int,
    limit: int,
    after_id: int | None,
    *,
    before_id: int | None = None,
    skip_history: bool = False,
    stream_ids: list[int] | None = None,
    since_updated: str | None = None,
) -> list[dict]:
    _finalize_stale_streams(conn, room["id"])
    history_filter = ""
    history_params: list = []
    if (
        not skip_history
        and not _is_room_governor(room, user_id)
        and member
        and not member["can_view_history"]
    ):
        history_filter = "AND m.id > ?"
        history_params.append(member["first_visible_msg_id"])
    if after_id is None:
        if before_id is not None:
            rows = conn.execute(
                f"""
                {MESSAGE_SELECT}
                WHERE m.room_id = ? {history_filter} AND m.id < ?
                ORDER BY m.id DESC LIMIT ?
                """,
                (room["id"], *history_params, before_id, limit),
            ).fetchall()
            return _visible_rows(reversed(rows), room, user_id)
        rows = conn.execute(
            f"""
            {MESSAGE_SELECT}
            WHERE m.room_id = ? {history_filter}
            ORDER BY m.id DESC LIMIT ?
            """,
            (room["id"], *history_params, limit),
        ).fetchall()
        return _visible_rows(reversed(rows), room, user_id)

    stream_ids = stream_ids or []
    extra_sql = ""
    extra_params: list = []
    if stream_ids:
        placeholders = ",".join("?" * len(stream_ids))
        extra_sql = f" OR m.id IN ({placeholders})"
        extra_params.extend(stream_ids)
        if since_updated:
            extra_sql = f" OR (m.id IN ({placeholders}) AND m.updated_at > ?)"
            extra_params.append(since_updated)

    rows = conn.execute(
        f"""
        {MESSAGE_SELECT}
        WHERE m.room_id = ? AND (m.id > ?{extra_sql}) {history_filter}
        ORDER BY m.id ASC LIMIT ?
        """,
        (room["id"], after_id, *extra_params, *history_params, limit),
    ).fetchall()
    return _visible_rows(rows, room, user_id)


@app.get("/api/rooms/{room_name}/messages")
async def recent_messages(
    room_name: str,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    after_id: Annotated[int | None, Query(alias="afterId", ge=0)] = None,
    before_id: Annotated[int | None, Query(alias="beforeId", ge=0)] = None,
    wait: Annotated[int, Query(ge=0, le=MAX_LONG_POLL_SECONDS)] = 0,
    stream_ids: Annotated[str | None, Query(alias="streamIds")] = None,
    since_updated_at: Annotated[str | None, Query(alias="sinceUpdatedAt", max_length=40)] = None,
):
    """读消息。`afterId` 增量；`beforeId` 向前翻页（仅当不带 afterId 时生效，供 3D 渲染端回填历史）；`wait` 长轮询；`streamIds`+`sinceUpdatedAt` 用来拉取仍在流式更新的旧消息。"""
    wanted_ids = _parse_stream_ids(stream_ids)
    since = (since_updated_at or "").strip() or None
    with get_db() as conn:
        room, member = _require_membership(conn, room_name, user["id"])
        rows = _fetch_messages(
            conn, room, member, user["id"], limit, after_id,
            before_id=before_id,
            stream_ids=wanted_ids, since_updated=since,
        )
        _mark_room_read(conn, room["id"], user["id"])
        _bump_my_update(conn, room, user["id"], _newest_visible_at(room, user["id"], rows))
        items = _message_dicts(conn, rows, room["name"], room=room, user_id=user["id"])
        room_id = room["id"]
        room_label = room["name"]
    if wait and after_id is not None and not rows:
        ev = _room_event(room_id)
        try:
            await asyncio.wait_for(ev.wait(), timeout=wait)
        except asyncio.TimeoutError:
            return {"roomName": room_label, "messages": []}
        with get_db() as conn:
            room, member = _require_membership(conn, room_name, user["id"])
            rows = _fetch_messages(
                conn, room, member, user["id"], limit, after_id,
                stream_ids=wanted_ids, since_updated=since,
            )
            _mark_room_read(conn, room["id"], user["id"])
            _bump_my_update(conn, room, user["id"], _newest_visible_at(room, user["id"], rows))
            items = _message_dicts(conn, rows, room["name"], room=room, user_id=user["id"])
            room_label = room["name"]
    return {"roomName": room_label, "messages": items}


@app.post("/api/rooms/{room_name}/messages")
def send_message(room_name: str, body: MessageCreate, user: CurrentUser):
    with get_db() as conn:
        room, member = _require_membership(conn, room_name, user["id"])
        targets = _whisper_targets(conn, room, body.content, user["id"])
        mention = _mention_target(conn, room, body.content)
        _check_whisper_targets(conn, room["id"], user["username"], targets)
        _check_action_allowed(room, member, user["id"], "speak", whisper=bool(targets))
        whisper_to, whisper_to_ids = _whisper_columns(targets)
        reply_to = _resolve_reply(conn, room, user, body.replyTo)
        now = _db_now(conn)
        conn.execute(
            """
            INSERT INTO messages (room_id, user_id, content, whisper_to, whisper_to_ids, mention_to, reply_to, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (room["id"], user["id"], body.content, whisper_to, whisper_to_ids,
             mention["id"] if mention else None, reply_to, now),
        )
        row = _load_message(conn, conn.execute("SELECT last_insert_rowid() AS mid").fetchone()["mid"])
        _bump_my_update(conn, room, user["id"], _newest_visible_at(room, user["id"], [row]))
        item = _message_dicts(conn, [row], room["name"], room=room, user_id=user["id"])[0]
        room_id = room["id"]
    notify_room(room_id)
    return item


@app.post("/api/rooms/{room_name}/messages/stream")
def start_stream(room_name: str, user: CurrentUser, body: StreamStart = StreamStart()):
    """创建一条流式文本消息（可空开头）。随后用同一条 id 追加 delta，最后 done=true。"""
    payload = body or StreamStart()
    with get_db() as conn:
        room, member = _require_membership(conn, room_name, user["id"])
        targets = _whisper_targets(conn, room, payload.content or "", user["id"])
        mention = _mention_target(conn, room, payload.content or "")
        _check_whisper_targets(conn, room["id"], user["username"], targets)
        _check_action_allowed(room, member, user["id"], "speak", whisper=bool(targets))
        whisper_to, whisper_to_ids = _whisper_columns(targets)
        reply_to = _resolve_reply(conn, room, user, payload.replyTo)
        now = _db_now(conn)
        conn.execute(
            """
            INSERT INTO messages (room_id, user_id, content, whisper_to, whisper_to_ids, mention_to, reply_to, streaming, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (room["id"], user["id"], payload.content or "", whisper_to, whisper_to_ids,
             mention["id"] if mention else None, reply_to, now),
        )
        row = _load_message(conn, conn.execute("SELECT last_insert_rowid() AS mid").fetchone()["mid"])
        _bump_my_update(conn, room, user["id"], _newest_visible_at(room, user["id"], [row]))
        item = _message_dicts(conn, [row], room["name"], room=room, user_id=user["id"])[0]
        room_id = room["id"]
    notify_room(room_id)
    return item


@app.post("/api/rooms/{room_name}/messages/{message_id}/stream")
def patch_stream(room_name: str, message_id: int, body: StreamPatch, user: CurrentUser):
    """追加 `delta`、用 `content` 整段替换，或 `done=true` 结束流式。仅作者可写。"""
    if body.delta is not None and body.content is not None:
        raise HTTPException(status_code=400, detail="delta 与 content 不能同时使用")
    if body.delta is None and body.content is None and not body.done:
        raise HTTPException(status_code=400, detail="请提供 delta、content 或 done")
    with get_db() as conn:
        room, member = _require_membership(conn, room_name, user["id"])
        _finalize_stale_streams(conn, room["id"])
        row = conn.execute(
            "SELECT id, user_id, content, msg_type, streaming, whisper_to, whisper_to_ids FROM messages WHERE id = ? AND room_id = ?",
            (message_id, room["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="消息不存在")
        if row["user_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="只有作者可以更新该流式消息")
        if row["msg_type"] != "text":
            raise HTTPException(status_code=400, detail="只有文本消息支持流式更新")
        if not row["streaming"]:
            raise HTTPException(status_code=409, detail="该消息已结束流式，不能再追加")
        _check_action_allowed(room, member, user["id"], "speak",
                              whisper=bool(row["whisper_to_ids"]))
        content = row["content"] or ""
        if body.content is not None:
            content = body.content
        elif body.delta is not None:
            content = content + body.delta
        if len(content) > 64000:
            raise HTTPException(status_code=400, detail="消息超过 64000 字上限")
        # 内容变化时重解析 @@ 前缀：新增前缀要重新过私聊规则并改写接收者；
        # 去掉前缀时保留原私聊属性（可见性只紧不松），防止私聊内容被公开广播
        targets = _whisper_targets(conn, room, content, user["id"])
        mention = _mention_target(conn, room, content)
        if targets:
            _check_whisper_targets(conn, room["id"], user["username"], targets)
            whisper_to, whisper_to_ids = _whisper_columns(targets)
        else:
            whisper_to, whisper_to_ids = row["whisper_to"], row["whisper_to_ids"]
        streaming = 0 if body.done else 1
        conn.execute(
            """
            UPDATE messages
            SET content = ?, whisper_to = ?, whisper_to_ids = ?, mention_to = ?, streaming = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now')
            WHERE id = ?
            """,
            (content, whisper_to, whisper_to_ids, mention["id"] if mention else None, streaming, message_id),
        )
        row = _load_message(conn, message_id)
        item = _message_dicts(conn, [row], room["name"], room=room, user_id=user["id"])[0]
        room_id = room["id"]
    notify_room(room_id)
    return item


@app.delete("/api/rooms/{room_name}/messages/{message_id}")
def recall_message(room_name: str, message_id: int, user: CurrentUser):
    """撤回自己发出的、本房间最后一条消息（之后没有任何新消息即可，不限时长）：
    墓碑化（清空内容 / 附件 / 私聊 / 引用）。

    保留 id 与 created_at，增量轮询（afterId）游标不乱；其他客户端通过
    streamIds + sinceUpdatedAt 拿到 recalled 行后删除对应气泡（前端会把
    最后一条消息的 id 常驻 streamIds，所以任意时刻的撤回都能同步到）。
    """
    with get_db() as conn:
        room, member = _require_membership(conn, room_name, user["id"])
        row = conn.execute(
            """
            SELECT id, user_id, attachment_path, recalled
            FROM messages WHERE id = ? AND room_id = ?
            """,
            (message_id, room["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="消息不存在")
        if row["user_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="只能撤回自己发送的消息")
        if not row["recalled"]:
            newer = conn.execute(
                "SELECT 1 FROM messages WHERE room_id = ? AND id > ? LIMIT 1",
                (room["id"], message_id),
            ).fetchone()
            if newer:
                raise HTTPException(status_code=403, detail="后面已有新消息，只能撤回本房间最后一条消息")
            if row["attachment_path"]:
                try:
                    (UPLOADS_DIR / row["attachment_path"]).unlink(missing_ok=True)
                except OSError:
                    pass
            conn.execute(
                """
                UPDATE messages
                SET content = '', msg_type = 'text', attachment_name = NULL, attachment_path = NULL,
                    whisper_to = NULL, whisper_to_ids = NULL, mention_to = NULL, reply_to = NULL, duration_ms = NULL,
                    streaming = 0, recalled = 1,
                    updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                """,
                (message_id,),
            )
        row = _load_message(conn, message_id)
        item = _message_dicts(conn, [row], room["name"], room=room, user_id=user["id"])[0]
        room_id = room["id"]
    notify_room(room_id)
    return item


_SAFE_FILENAME = re.compile(r"[^\w.\-]+")
def _sanitize_filename(name: str | None) -> str:
    base = Path(name or "").name
    base = _SAFE_FILENAME.sub("_", base).strip("._")
    return base[:128] or "file"


def _is_image_upload(filename: str, content_type: str | None, data: bytes) -> bool:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in IMAGE_TYPES:
        return True
    if Path(filename).suffix.lower() in IMAGE_EXTS:
        return True
    if data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff"):
        return True
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return True
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return True
    return False


def _audio_ext(filename: str | None, content_type: str | None, data: bytes) -> str | None:
    """识别音频类型，返回规范化扩展名；不是音频返回 None。

    依次看 content-type、文件扩展名、魔数（webm/ogg/wav/mp3/mp4）。
    """
    ctype = (content_type or "").split(";")[0].strip().lower()
    by_type = {
        "audio/webm": ".webm", "video/webm": ".webm",
        "audio/ogg": ".ogg", "application/ogg": ".ogg",
        "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
        "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "video/mp4": ".mp4",
        "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav",
        "audio/aac": ".aac",
    }
    if ctype in by_type:
        return by_type[ctype]
    ext = Path(filename or "").suffix.lower()
    if ext in AUDIO_MIME_BY_EXT:
        return ext
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return ".webm"
    if data.startswith(b"OggS"):
        return ".ogg"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return ".wav"
    if data.startswith(b"ID3") or (len(data) > 2 and (data[0], data[1] & 0xE0) == (0xFF, 0xE0)):
        return ".mp3"
    if len(data) > 12 and data[4:8] == b"ftyp":
        return ".m4a"
    return None


def _voice_media_type(filename: str) -> str:
    return AUDIO_MIME_BY_EXT.get(Path(filename).suffix.lower(), "audio/webm")


def _file_response(path: Path, filename: str, inline: bool, media_type: str | None = None) -> FileResponse:
    media = media_type or mimetypes.guess_type(filename)[0]
    return FileResponse(
        path,
        filename=filename,
        media_type=media or ("application/octet-stream" if not inline else "image/jpeg"),
        content_disposition_type="inline" if inline else "attachment",
    )


@app.post("/api/rooms/{room_name}/attachments")
async def upload_attachment(room_name: str, user: CurrentUser, file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail="附件超过 20MB 上限")
    safe_name = _sanitize_filename(file.filename)
    msg_type = "image" if _is_image_upload(safe_name, file.content_type, data) else "attachment"
    with get_db() as conn:
        room, member = _require_membership(conn, room_name, user["id"])
        _check_action_allowed(room, member, user["id"], "upload")
        now = _db_now(conn)
        conn.execute(
            """
            INSERT INTO messages (room_id, user_id, content, msg_type, attachment_name, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (room["id"], user["id"], safe_name, msg_type, safe_name, now),
        )
        message_id = conn.execute("SELECT last_insert_rowid() AS mid").fetchone()["mid"]
        rel_path = f"{room['id']}/{message_id}-{safe_name}"
        dest = UPLOADS_DIR / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        conn.execute("UPDATE messages SET attachment_path = ? WHERE id = ?", (rel_path, message_id))
        row = _load_message(conn, message_id)
        _bump_my_update(conn, room, user["id"], _newest_visible_at(room, user["id"], [row]))
        item = _message_dicts(conn, [row], room["name"], room=room, user_id=user["id"])[0]
        room_id = room["id"]
    notify_room(room_id)
    return item


@app.post("/api/rooms/{room_name}/voice")
async def send_voice(
    room_name: str,
    user: CurrentUser,
    file: UploadFile = File(...),
    text: Annotated[str, Form()] = "",
    reply_to: Annotated[int | None, Form(alias="replyTo")] = None,
    duration_ms: Annotated[int | None, Form(alias="durationMs", ge=0, le=600_000)] = None,
):
    """发送语音消息：录制的音频（MediaRecorder）+ 同时段 ASR 识别文本。

    `text` 可带 `@@用户名` 前缀表示私聊（与文本消息同语法）；`replyTo` 支持引用回复。
    """
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="语音内容为空")
    if len(data) > MAX_VOICE_BYTES:
        raise HTTPException(status_code=413, detail="语音超过 10MB 上限")
    ext = _audio_ext(file.filename, file.content_type, data)
    if ext is None:
        raise HTTPException(status_code=400, detail="语音必须是音频文件（webm/ogg/mp4/mp3/wav/aac 等）")
    text = (text or "").strip()
    if len(text) > 64000:
        raise HTTPException(status_code=400, detail="语音识别文本超过 64000 字上限")
    with get_db() as conn:
        room, member = _require_membership(conn, room_name, user["id"])
        targets = _whisper_targets(conn, room, text, user["id"])
        _check_whisper_targets(conn, room["id"], user["username"], targets)
        _check_action_allowed(room, member, user["id"], "speak", whisper=bool(targets))
        whisper_to, whisper_to_ids = _whisper_columns(targets)
        reply_to_id = _resolve_reply(conn, room, user, reply_to)
        now = _db_now(conn)
        safe_name = f"voice{ext}"   # ext 自带前导点（.webm/.m4a/...）
        conn.execute(
            """
            INSERT INTO messages (room_id, user_id, content, msg_type, attachment_name,
                                  whisper_to, whisper_to_ids, reply_to, duration_ms, updated_at)
            VALUES (?, ?, ?, 'voice', ?, ?, ?, ?, ?, ?)
            """,
            (room["id"], user["id"], text, safe_name, whisper_to, whisper_to_ids, reply_to_id, duration_ms, now),
        )
        message_id = conn.execute("SELECT last_insert_rowid() AS mid").fetchone()["mid"]
        rel_path = f"{room['id']}/{message_id}-{safe_name}"
        dest = UPLOADS_DIR / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        conn.execute("UPDATE messages SET attachment_path = ? WHERE id = ?", (rel_path, message_id))
        row = _load_message(conn, message_id)
        _bump_my_update(conn, room, user["id"], _newest_visible_at(room, user["id"], [row]))
        item = _message_dicts(conn, [row], room["name"], room=room, user_id=user["id"])[0]
        room_id = room["id"]
    notify_room(room_id)
    return item


@app.patch("/api/rooms/{room_name}/voice/{message_id}/text")
def patch_voice_text(room_name: str, message_id: int, body: VoiceTextPatch, user: CurrentUser):
    """为空文本语音补写识别文本（ASR）：作者本人或作者名下 Agent。

    场景：发送端浏览器没有语音识别（终端浏览器/头显等），语音 `content` 为空，
    主人的 Agent 本地转写后补回文本；确实识别不出字时补「（空）」。
    已有正文（或「（空）」以外的任何文本）不允许覆盖（409）；补写不改变消息
    发送时的可见性——空文本语音必为公开消息，故拒绝 @@/# 前缀。
    """
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="识别文本不能为空")
    if text.startswith("@@") or text.startswith("#"):
        raise HTTPException(status_code=400, detail="补写文本不能带 @@/# 私聊前缀（该消息发送时是公开的）")
    with get_db() as conn:
        room, _mem = _require_membership(conn, room_name, user["id"])
        caller = _get_user(conn, user["username"])
        row = conn.execute(
            "SELECT id, user_id, msg_type, content, recalled FROM messages WHERE id = ? AND room_id = ?",
            (message_id, room["id"]),
        ).fetchone()
        if not row or row["recalled"]:
            raise HTTPException(status_code=404, detail="消息不存在")
        if row["user_id"] != caller["id"] and not (
            caller["kind"] == "agent" and caller["owner_id"] == row["user_id"]
        ):
            raise HTTPException(status_code=403, detail="只有语音作者或作者名下 Agent 可以补写识别文本")
        if row["msg_type"] != "voice":
            raise HTTPException(status_code=400, detail="只有语音消息可以补写识别文本")
        if (row["content"] or "").strip() not in ("", "（空）"):
            raise HTTPException(status_code=409, detail="该语音已有识别文本，不能覆盖")
        conn.execute(
            "UPDATE messages SET content = ?, updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now') WHERE id = ?",
            (text, message_id),
        )
        row = _load_message(conn, message_id)
        item = _message_dicts(conn, [row], room["name"], room=room, user_id=user["id"])[0]
        room_id = room["id"]
    notify_room(room_id)
    return item


@app.get("/api/rooms/{room_name}/attachments/{message_id}")
def download_attachment(room_name: str, message_id: int, user: CurrentUser):
    with get_db() as conn:
        room, _mem = _require_membership(conn, room_name, user["id"])
        row = conn.execute(
            """
            SELECT m.attachment_name, m.attachment_path, m.msg_type, m.user_id,
                   m.whisper_to, m.whisper_to_ids
            FROM messages m
            JOIN rooms r ON r.id = m.room_id
            WHERE m.id = ? AND r.id = ? AND r.archived_at IS NULL AND m.msg_type IN ('attachment', 'image', 'voice')
            """,
            (message_id, room["id"]),
        ).fetchone()
        _require_file_visible(row, room, user["id"])
        if not row["attachment_path"]:
            raise HTTPException(status_code=404, detail="附件不存在")
        path = UPLOADS_DIR / row["attachment_path"]
        if not path.is_file():
            raise HTTPException(status_code=404, detail="附件文件缺失")
        media = _voice_media_type(row["attachment_name"]) if row["msg_type"] == "voice" else None
    return _file_response(path, row["attachment_name"], inline=row["msg_type"] in ("image", "voice"), media_type=media)


# ---------- 共同文件（room_files）----------
# 房间级共享文件列表：只存最新状态（LWW），无历史版本。内容落盘 data/files/<room_id>/<file_id><ext>，
# 元数据入 room_files 表；2D / XR / Agent 三端同一套 API，变更感知靠 rooms.files_revision + notify_room。

FILE_SELECT = """
        SELECT f.*, cu.username AS createdByName, uu.username AS updatedByName,
               wu.username AS worldUpdatedByName
        FROM room_files f
        JOIN users cu ON cu.id = f.created_by
        LEFT JOIN users uu ON uu.id = f.updated_by
        LEFT JOIN users wu ON wu.id = f.world_updated_by
"""

_CTRL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _get_file(conn, room_id: int, file_id: int):
    return conn.execute(
        FILE_SELECT + " WHERE f.room_id = ? AND f.id = ?", (room_id, file_id)
    ).fetchone()


def _files_revision(conn, room_id: int) -> int:
    return conn.execute(
        "SELECT files_revision FROM rooms WHERE id = ?", (room_id,)
    ).fetchone()["files_revision"]


def _bump_files_revision(conn, room_id: int) -> None:
    conn.execute("UPDATE rooms SET files_revision = files_revision + 1 WHERE id = ?", (room_id,))


def _check_file_edit_allowed(room, member, user_id: int) -> None:
    """文件编辑权 = 治理者恒放行 → 房间锁定 403 → 成员级 can_edit_files 403。

    镜像 _check_action_allowed 的 muted/can_speak 合成；与发言/附件权限完全正交。
    读取（列表/内容）不进此判定，只查成员身份。
    """
    if _is_room_governor(room, user_id):
        return
    if room["files_locked"]:
        raise HTTPException(status_code=403, detail="房间共同文件已锁定")
    if not member["can_edit_files"]:
        raise HTTPException(status_code=403, detail="你已被禁止编辑房间文件")


def _files_summary(conn, room, member, user_id: int) -> dict:
    return {
        "revision": room["files_revision"],
        "locked": bool(room["files_locked"]),
        "canEdit": _is_room_governor(room, user_id)
        or (not room["files_locked"] and bool(member["can_edit_files"])),
        "count": conn.execute(
            "SELECT COUNT(*) AS c FROM room_files WHERE room_id = ?", (room["id"],)
        ).fetchone()["c"],
    }


def _validate_file_name(name: str) -> str:
    """共同文件显示名校验：去路径分量、拒控制字符、1–128 字符。

    与附件的 _sanitize_filename 不同：显示名不进磁盘路径（路径只由 room_id/file_id
    组成），允许中文与空格，无需激进替换。
    """
    base = Path(name or "").name.strip()
    if not base:
        raise HTTPException(status_code=400, detail="文件名不能为空")
    if _CTRL_CHARS_RE.search(base):
        raise HTTPException(status_code=400, detail="文件名不能包含控制字符")
    if len(base) > 128:
        raise HTTPException(status_code=400, detail="文件名过长（最多 128 字符）")
    return base


def _file_storage_ext(name: str) -> str:
    """磁盘路径里的扩展名：仅接受安全的短字母数字扩展名，其余置空（人工排障用，不参与寻址）。"""
    ext = Path(name).suffix.lower()
    return ext if re.fullmatch(r"\.[\w]{1,16}", ext) else ""


def _image_mime_from_data(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _classify_file(name: str, declared_mime: str | None, data: bytes) -> tuple[str, str]:
    """服务器权威分类（kind, mime），按「魔数 → 扩展名 → 声明 mime」判定。

    注意不能整用 _gltf_mime：它的 `{` 起头分支会把任意 JSON 文本当成 glTF——
    那个 helper 服务于「上传者本来就要传 3D 文件」的场景，这里 JSON 是常见文本类型。
    """
    ext = Path(name or "").suffix.lower()
    ctype = (declared_mime or "").split(";")[0].strip().lower()

    if data.startswith(b"glTF"):
        return "model", ("model/vrm" if _glb_looks_vrm(data) else "model/gltf-binary")
    if ext == ".vrm":
        return "model", "model/vrm"
    if ext == ".glb":
        return "model", "model/gltf-binary"
    if ext == ".gltf":
        return "model", "model/gltf+json"
    magic_mime = _image_mime_from_data(data)
    if magic_mime:
        return "image", magic_mime
    if ext in IMAGE_EXTS:
        return "image", IMAGE_MIME_BY_EXT.get(ext) or (ctype if ctype in IMAGE_TYPES else "image/png")
    if ext == ".svg" or ctype == "image/svg+xml":
        return "svg", "image/svg+xml"
    if ext in VIDEO_FILE_EXTS:
        return "video", VIDEO_MIME_BY_EXT.get(ext, "video/mp4")
    # webm/mp4 的魔数与音频重叠，只有声明 video/* 时魔数才可信为视频
    if data.startswith(b"\x1a\x45\xdf\xa3") and ctype.startswith("video/"):
        return "video", "video/webm"
    if len(data) > 12 and data[4:8] == b"ftyp" and ctype.startswith("video/"):
        return "video", "video/mp4"
    audio_ext = _audio_ext(name, declared_mime, data)
    if audio_ext:
        return "audio", AUDIO_MIME_BY_EXT.get(audio_ext, "audio/webm")
    if ext in (".md", ".markdown"):
        return "markdown", "text/markdown"
    declared_text = ctype.startswith("text/") or ctype in TEXT_FILE_MIMES
    if ext in TEXT_FILE_EXTS or declared_text:
        return "text", (ctype if declared_text else (mimetypes.guess_type(name)[0] or "text/plain"))
    return "other", (ctype or "application/octet-stream")


def _reclassify_file(kind: str, mime: str, name: str) -> tuple[str, str]:
    """改名后按「新扩展名 + 存量 mime」重算 kind/mime，不重读内容字节。"""
    ext = Path(name).suffix.lower()
    if ext == ".vrm":
        return "model", "model/vrm"
    if ext == ".glb":
        return "model", "model/gltf-binary"
    if ext == ".gltf":
        return "model", "model/gltf+json"
    if ext == ".svg":
        return "svg", "image/svg+xml"
    if ext in VIDEO_FILE_EXTS:
        return "video", VIDEO_MIME_BY_EXT.get(ext, "video/mp4")
    if ext in AUDIO_MIME_BY_EXT:
        return "audio", AUDIO_MIME_BY_EXT[ext]
    if ext in IMAGE_EXTS:
        return "image", IMAGE_MIME_BY_EXT.get(ext) or mime
    if ext in (".md", ".markdown"):
        return "markdown", "text/markdown"
    if ext in TEXT_FILE_EXTS:
        return "text", mimetypes.guess_type(name)[0] or "text/plain"
    # 未知扩展名：保持既有分类（model.glb → model.fbx 仍是 model）
    return kind, mime


def _file_size_limit(kind: str) -> int:
    return MAX_FILE_TEXT_BYTES if kind in ("markdown", "text", "svg") else MAX_FILE_BYTES


def _write_file_bytes(dest: Path, data: bytes) -> None:
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dest)


def _unlink_file_content(rel_path: str | None) -> None:
    if not rel_path:
        return
    try:
        (FILES_DIR / rel_path).unlink(missing_ok=True)
    except OSError:
        pass  # 磁盘清理失败不阻断删行（沿用撤回附件的容错风格）


def _file_dict(row, room_name: str, archive_id: int | None = None) -> dict:
    version = quote(str(_row_get(row, "updated_at") or ""), safe="")
    if archive_id is not None:
        content_url = f"/api/archives/{archive_id}/files/{row['id']}/content?v={version}"
    else:
        content_url = f"/api/rooms/{room_name}/files/{row['id']}/content?v={version}"
    world = None
    if row["kind"] == "model" and _row_get(row, "world_updated_at"):
        pose = None
        raw_pose = _row_get(row, "world_pose")
        if raw_pose:
            try:
                pose = json.loads(raw_pose)
            except (ValueError, TypeError):
                pose = None
        world = {
            "visible": bool(row["world_visible"]),
            "pose": pose,
            "updatedBy": row["worldUpdatedByName"],
            "updatedAt": row["world_updated_at"],
        }
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "mime": row["mime"],
        "size": row["size"],
        "description": _row_get(row, "description"),
        "createdBy": row["createdByName"],
        "createdAt": row["created_at"],
        "updatedBy": _row_get(row, "updatedByName") or row["createdByName"],
        "updatedAt": row["updated_at"],
        "contentUrl": content_url,
        "world": world,
        "source": (
            {
                "resourceId": _row_get(row, "source_resource_id"),
                "resourceVersionId": _row_get(row, "source_resource_version_id"),
                "licenseId": _row_get(row, "source_license_id"),
                "purchasedBy": _row_get(row, "source_purchased_by"),
                "detached": bool(_row_get(row, "source_detached", 0)),
            }
            if _row_get(row, "source_resource_id") is not None
            else None
        ),
    }


def _files_payload(conn, room) -> dict:
    rows = conn.execute(
        FILE_SELECT + " WHERE f.room_id = ? ORDER BY f.updated_at DESC, f.id ASC",
        (room["id"],),
    ).fetchall()
    return {
        "roomName": room["name"],
        "revision": room["files_revision"],
        "files": [_file_dict(row, room["name"]) for row in rows],
    }


def _single_file_payload(conn, room_name: str, room_id: int, file_id: int) -> dict:
    return {
        "roomName": room_name,
        "revision": _files_revision(conn, room_id),
        "file": _file_dict(_get_file(conn, room_id, file_id), room_name),
    }


async def _read_file_request_body(request: Request):
    """创建/替换端点的双格式请求体：JSON `{name, content, ...}`（文本直写）或
    multipart（`file` + 可选 `name`/`description`/`baseUpdatedAt`）。返回
    (data_bytes, raw_name, declared_mime, description, base_updated_at, is_json)。"""
    content_type = (request.headers.get("content-type") or "").lower()
    if content_type.startswith("multipart/"):
        form = await request.form()
        upload = form.get("file")
        # request.form() 返回的是 starlette 的 UploadFile（fastapi.UploadFile 是其子类，
        # 反向 isinstance 不成立），这里按 starlette 类判断
        if not isinstance(upload, StarletteUploadFile):
            raise HTTPException(status_code=400, detail="multipart 请求需要 file 字段")
        data = await upload.read()
        raw_name = (str(form.get("name")) if isinstance(form.get("name"), str) else "") or (upload.filename or "")
        declared = upload.content_type
        description = str(form.get("description")) if isinstance(form.get("description"), str) else None
        base = str(form.get("baseUpdatedAt")) if isinstance(form.get("baseUpdatedAt"), str) else None
        return data, raw_name, declared, description, base, False
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 或 multipart") from exc
    try:
        if isinstance(payload, dict) and "content" in payload and "name" not in payload:
            body = FileReplace.model_validate(payload)
            name, description = None, None
        else:
            body = FileCreate.model_validate(payload)
            name, description = body.name, body.description
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"参数错误：{exc.errors()[0].get('msg', '无效请求')}") from exc
    return (
        body.content.encode("utf-8"),
        name,
        None,
        description,
        getattr(body, "baseUpdatedAt", None),
        True,
    )


@app.get("/api/rooms/{room_name}/files")
async def list_room_files(
    room_name: str,
    user: CurrentUser,
    since_revision: Annotated[int | None, Query(alias="sinceRevision", ge=0)] = None,
    wait: Annotated[int, Query(ge=0, le=MAX_LONG_POLL_SECONDS)] = 0,
):
    """房间共同文件列表。`sinceRevision`+`wait` 长轮询：revision 未变则挂起等待，
    任何文件/锁定变更经 notify_room 唤醒（消息等活动也会假唤醒，醒来多比对一次 revision 无害）。"""
    with get_db() as conn:
        room, _member = _require_membership(conn, room_name, user["id"])
        payload = _files_payload(conn, room)
        room_id = room["id"]
    if wait and since_revision is not None and payload["revision"] == since_revision:
        ev = _room_event(room_id)
        try:
            await asyncio.wait_for(ev.wait(), timeout=wait)
        except asyncio.TimeoutError:
            return payload
        with get_db() as conn:
            room = _get_room_by_id(conn, room_id)
            if not room or _is_ended(room):
                raise HTTPException(status_code=410, detail="房间已结束")
            payload = _files_payload(conn, room)
    return payload


@app.post("/api/rooms/{room_name}/files")
async def create_room_file(room_name: str, request: Request, user: CurrentUser):
    data, raw_name, declared_mime, description, _base, is_json = await _read_file_request_body(request)
    if not raw_name:
        raise HTTPException(status_code=400, detail="缺少文件名")
    file_name = _validate_file_name(raw_name)
    if not data:
        raise HTTPException(status_code=400, detail="文件内容为空")
    kind, mime = _classify_file(file_name, declared_mime, data)
    if is_json and kind not in ("markdown", "text", "svg"):
        raise HTTPException(
            status_code=400,
            detail="JSON 直写仅支持文本类（.md/.txt/.json 等）；二进制文件请用 multipart 上传",
        )
    limit = _file_size_limit(kind)
    if len(data) > limit:
        detail = "文本文件超过 2MB 上限" if limit == MAX_FILE_TEXT_BYTES else "文件超过 50MB 上限"
        raise HTTPException(status_code=413, detail=detail)
    with get_db() as conn:
        room, member = _require_membership(conn, room_name, user["id"])
        _check_file_edit_allowed(room, member, user["id"])
        exists = conn.execute(
            "SELECT 1 FROM room_files WHERE room_id = ? AND name = ? COLLATE NOCASE",
            (room["id"], file_name),
        ).fetchone()
        if exists:
            raise HTTPException(status_code=409, detail="同名文件已存在")
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM room_files WHERE room_id = ?", (room["id"],)
        ).fetchone()["c"]
        if count >= MAX_FILES_PER_ROOM:
            raise HTTPException(status_code=400, detail=f"房间文件数已达上限（{MAX_FILES_PER_ROOM}）")
        now = _db_now(conn)
        conn.execute(
            """
            INSERT INTO room_files (room_id, name, kind, mime, size, description,
                                    created_by, created_at, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (room["id"], file_name, kind, mime, len(data), description, user["id"], now, user["id"], now),
        )
        file_id = conn.execute("SELECT last_insert_rowid() AS fid").fetchone()["fid"]
        rel_path = f"{room['id']}/{file_id}{_file_storage_ext(file_name)}"
        dest = FILES_DIR / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        _write_file_bytes(dest, data)
        conn.execute("UPDATE room_files SET content_path = ? WHERE id = ?", (rel_path, file_id))
        _bump_files_revision(conn, room["id"])
        payload = _single_file_payload(conn, room["name"], room["id"], file_id)
        room_id = room["id"]
    notify_room(room_id)
    return payload


@app.get("/api/rooms/{room_name}/files/{file_id}")
def get_room_file(room_name: str, file_id: int, user: CurrentUser):
    with get_db() as conn:
        room, _member = _require_membership(conn, room_name, user["id"])
        row = _get_file(conn, room["id"], file_id)
        if not row:
            raise HTTPException(status_code=404, detail="文件不存在")
        return _single_file_payload(conn, room["name"], room["id"], file_id)


@app.get("/api/rooms/{room_name}/files/{file_id}/content")
def download_room_file_content(
    room_name: str,
    file_id: int,
    user: CurrentUser,
    download: bool = False,
):
    with get_db() as conn:
        room, _mem = _require_membership(conn, room_name, user["id"])
        row = _get_file(conn, room["id"], file_id)
        if not row or not row["content_path"]:
            raise HTTPException(status_code=404, detail="文件不存在")
        path = FILES_DIR / row["content_path"]
        if not path.is_file():
            raise HTTPException(status_code=404, detail="文件内容缺失")
        mime = row["mime"]
        name = row["name"]
    return _file_response(path, name, inline=not download, media_type=mime)


@app.put("/api/rooms/{room_name}/files/{file_id}")
async def replace_room_file(room_name: str, file_id: int, request: Request, user: CurrentUser):
    data, _raw_name, declared_mime, _desc, base_updated_at, is_json = await _read_file_request_body(request)
    if not data:
        raise HTTPException(status_code=400, detail="文件内容为空")
    with get_db() as conn:
        room, member = _require_membership(conn, room_name, user["id"])
        _check_file_edit_allowed(room, member, user["id"])
        row = _get_file(conn, room["id"], file_id)
        if not row:
            raise HTTPException(status_code=404, detail="文件不存在")
        if base_updated_at and base_updated_at != row["updated_at"]:
            updated_by = _row_get(row, "updatedByName") or row["createdByName"]
            raise HTTPException(status_code=409, detail=f"文件已被 {updated_by} 更新")
        kind, mime = _classify_file(row["name"], declared_mime, data)
        if is_json and kind not in ("markdown", "text", "svg"):
            raise HTTPException(
                status_code=400,
                detail="JSON 直写仅支持文本类（.md/.txt/.json 等）；二进制文件请用 multipart 上传",
            )
        if len(data) > _file_size_limit(kind):
            detail = "文本文件超过 2MB 上限" if kind in ("markdown", "text", "svg") else "文件超过 50MB 上限"
            raise HTTPException(status_code=413, detail=detail)
        now = _db_now(conn)
        # 整体替换：文件名与磁盘路径不变（路径由 room_id/file_id + 原名扩展名决定），kind/mime/size 重算
        rel_path = f"{room['id']}/{file_id}{_file_storage_ext(row['name'])}"
        dest = FILES_DIR / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        _write_file_bytes(dest, data)
        conn.execute(
            """
            UPDATE room_files
            SET kind = ?, mime = ?, size = ?, content_path = ?, updated_by = ?, updated_at = ?,
                source_detached = CASE WHEN source_resource_id IS NULL THEN source_detached ELSE 1 END
            WHERE id = ?
            """,
            (kind, mime, len(data), rel_path, user["id"], now, file_id),
        )
        _bump_files_revision(conn, room["id"])
        payload = _single_file_payload(conn, room["name"], room["id"], file_id)
        room_id = room["id"]
    notify_room(room_id)
    return payload


@app.patch("/api/rooms/{room_name}/files/{file_id}")
def update_room_file(room_name: str, file_id: int, body: FileMetaUpdate, user: CurrentUser):
    new_name = _blank_to_none(body.name)
    desc_provided = "description" in body.model_fields_set
    new_description = _blank_to_none(body.description) if desc_provided else None
    if not new_name and not desc_provided:
        raise HTTPException(status_code=400, detail="没有需要修改的字段")
    with get_db() as conn:
        room, member = _require_membership(conn, room_name, user["id"])
        _check_file_edit_allowed(room, member, user["id"])
        row = _get_file(conn, room["id"], file_id)
        if not row:
            raise HTTPException(status_code=404, detail="文件不存在")
        if new_name:
            file_name = _validate_file_name(new_name)
            if file_name.lower() != row["name"].lower():
                exists = conn.execute(
                    "SELECT 1 FROM room_files WHERE room_id = ? AND name = ? COLLATE NOCASE AND id != ?",
                    (room["id"], file_name, file_id),
                ).fetchone()
                if exists:
                    raise HTTPException(status_code=409, detail="同名文件已存在")
            kind, mime = _reclassify_file(row["kind"], row["mime"], file_name)
        else:
            file_name, kind, mime = row["name"], row["kind"], row["mime"]
        description = new_description if desc_provided else _row_get(row, "description")
        conn.execute(
            """
            UPDATE room_files SET name = ?, kind = ?, mime = ?, description = ? WHERE id = ?
            """,
            (file_name, kind, mime, description, file_id),
        )
        _bump_files_revision(conn, room["id"])
        payload = _single_file_payload(conn, room["name"], room["id"], file_id)
        room_id = room["id"]
    notify_room(room_id)
    return payload


@app.delete("/api/rooms/{room_name}/files/{file_id}")
def delete_room_file(room_name: str, file_id: int, user: CurrentUser):
    with get_db() as conn:
        room, member = _require_membership(conn, room_name, user["id"])
        _check_file_edit_allowed(room, member, user["id"])
        row = _get_file(conn, room["id"], file_id)
        if not row:
            raise HTTPException(status_code=404, detail="文件不存在")
        _unlink_file_content(row["content_path"])
        conn.execute("DELETE FROM room_files WHERE id = ?", (file_id,))
        _bump_files_revision(conn, room["id"])
        payload = {"roomName": room["name"], "revision": _files_revision(conn, room["id"]), "deleted": file_id}
        room_id = room["id"]
    notify_room(room_id)
    return payload


def _validate_pose_vec(values: list, label: str) -> list[float]:
    if len(values) != 3:
        raise HTTPException(status_code=400, detail=f"{label} 必须是 [x, y, z] 三元组")
    try:
        vec = [float(v) for v in values]
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{label} 必须是数字数组") from exc
    if not all(math.isfinite(v) for v in vec):
        raise HTTPException(status_code=400, detail=f"{label} 不能包含 NaN 或 Infinity")
    return vec


@app.put("/api/rooms/{room_name}/files/{file_id}/placement")
def set_file_placement(room_name: str, file_id: int, body: PlacementUpdate, user: CurrentUser):
    """3D 世界摆放（需求 10）：visible 开关 + 位姿 {position, rotation, scale}。

    LWW 无乐观锁（XR 拖拽期间客户端节流保存，并发互踩最多位姿跳变）；不改
    updated_at/contentUrl（内容乐观锁不受污染），只递增 revision。
    """
    pose_json = None
    if body.visible:
        if body.position is None:
            raise HTTPException(status_code=400, detail="visible=true 时必须提供 position")
        position = _validate_pose_vec(body.position, "position")
        rotation = _validate_pose_vec(body.rotation, "rotation") if body.rotation is not None else [0.0, 0.0, 0.0]
        scale = _validate_pose_vec(body.scale, "scale") if body.scale is not None else [1.0, 1.0, 1.0]
    with get_db() as conn:
        room, member = _require_membership(conn, room_name, user["id"])
        _check_file_edit_allowed(room, member, user["id"])
        row = _get_file(conn, room["id"], file_id)
        if not row:
            raise HTTPException(status_code=404, detail="文件不存在")
        if row["kind"] != "model":
            raise HTTPException(status_code=400, detail="只有 3D 模型文件可摆入房间")
        now = _db_now(conn)
        if body.visible:
            old_spin = 0.0
            if row["world_pose"]:
                try:
                    old_spin = float(json.loads(row["world_pose"]).get("spin") or 0.0)
                except Exception:
                    old_spin = 0.0
            spin = max(-360.0, min(360.0, float(body.spin))) if body.spin is not None else old_spin
            pose_json = json.dumps({"position": position, "rotation": rotation, "scale": scale, "spin": spin})
            others = conn.execute(
                "SELECT COUNT(*) AS c FROM room_files WHERE room_id = ? AND world_visible = 1 AND id != ?",
                (room["id"], file_id),
            ).fetchone()["c"]
            if others >= MAX_WORLD_MODELS:
                raise HTTPException(
                    status_code=400,
                    detail=f"同时常驻展示的 3D 模型已达上限（{MAX_WORLD_MODELS}），请先关闭其他模型",
                )
            conn.execute(
                """
                UPDATE room_files
                SET world_visible = 1, world_pose = ?, world_updated_by = ?, world_updated_at = ?
                WHERE id = ?
                """,
                (pose_json, user["id"], now, file_id),
            )
        else:
            # 关闭显示但保留位姿：再显示时按原位姿回来
            conn.execute(
                """
                UPDATE room_files
                SET world_visible = 0, world_updated_by = ?, world_updated_at = ?
                WHERE id = ?
                """,
                (user["id"], now, file_id),
            )
        _bump_files_revision(conn, room["id"])
        payload = _single_file_payload(conn, room["name"], room["id"], file_id)
        room_id = room["id"]
    notify_room(room_id)
    return payload


# ---------- 房间内 3D 位姿（presence）----------
# 两张表：room_presence 存每人最新位姿（全量快照来源），room_presence_log 按时间追加
# 变更（增量来源）。客户端首次拉全量、记下服务端返回的 serverTime 作为游标，之后每
# 0.5s 拉 since 之后的增量。人类位姿来自键盘/头显 6DoF；Agent 走同一接口自报，
# 由下面的步行速度校验兜底「不许瞬移」。
PRESENCE_RETENTION = "-10 minutes"
PRESENCE_RETENTION_SECS = 600.0      # 与上面 SQL 窗口保持一致（超窗的游标一律让客户端全量重取）
PRESENCE_WALK_SPEED = 2.2            # 允许的最大水平速度（米/秒，略高于人类快走）
PRESENCE_SPEED_SLACK = 0.8           # 速度校验容差（米）：抖动/丢包重传不至于被拒
PRESENCE_COALESCE_MS = 100           # 同人两次增量事件的合并间隔：与 10Hz 上报对齐（更密只刷新最新位姿）
PRESENCE_RADIUS_LIMIT = 40.0         # 位置绝对值上限（米）：远超房间尺寸即判非法
PRESENCE_Y_MIN, PRESENCE_Y_MAX = -2.0, 12.0
PRESENCE_MAX_HANDS = 2
PRESENCE_DELTA_LIMIT = 500
PRESENCE_STATE_CHARS = 500
# Agent 动画序列：动画是一次性的增量命令，不走 state（state 仍保持小状态上限）。
# 600 个关键帧约覆盖 60 秒的 10Hz 采样；关键帧本身可以是稀疏骨骼，避免强迫
# Agent 每拍重发 55 根骨骼。服务端仍限制 JSON 总量，避免把 presence 变成任意大文件通道。
PRESENCE_ANIMATION_MAX_KEYFRAMES = 600
PRESENCE_ANIMATION_MAX_DURATION = 300.0
PRESENCE_ANIMATION_MAX_BYTES = 900_000
PRESENCE_ANIMATION_ID_CHARS = 128

# ---------- 位姿流：二进制脏位增量 + 按距离分级 + 长轮询 ----------
# 目标：12 人房 10Hz 下每人下行约 5–6 KB/s（位置+朝向+双手+状态），并可承载全身骨骼、
# ARKit52 面部与一次性关键帧动画。加字段只改编解码，协议骨架不动。分级在**服务端**做：请求者的
# 最新位姿就在库里，按到各成员的水平距离决定这一帧给他带哪些字段（省的是真实带宽，
# 而不是只省客户端 CPU）。
PRESENCE_BIN_MAGIC = 0xB1              # 二进制帧首字节；客户端据此区分二进制与降级 JSON
PRESENCE_BIN_VERSION = 3
PRESENCE_LOD_NEAR_M = 5.0              # <5m 全量；5–15m 位置+朝向；>15m 仅位置
PRESENCE_LOD_MID_M = 15.0
PRESENCE_POS_EPS = 0.01                # 位置脏判定阈值（米）
PRESENCE_ANGLE_EPS = 0.02              # 朝向脏判定阈值（弧度，约 1.1°）
PRESENCE_HANDS_EPS = 0.005             # 手部脏判定阈值（米 / 四元数分量）
PRESENCE_PACK_POS = 100.0              # 位置量化：厘米（int16 → ±327m）
PRESENCE_PACK_ANGLE = 65536.0 / (2 * math.pi)   # 角度量化：int16 满量程 = 2π
PRESENCE_MAX_ENTRIES = 200             # 单帧成员上限

P_DIRTY_POS = 1
P_DIRTY_ORIENT = 2
P_DIRTY_HANDS = 4
P_DIRTY_STATE = 8
# 16 = 全身骨骼块、32 = ARKit52 面部块、64 = 一次性关键帧动画命令。

# Agent 位姿能力档（自报，服务端强制；人类客户端固定按 level 2 那档上报）：
#   1 = 只报位姿（头/身体位置 + 朝向）
#   2 = 再加双手 6DoF（与人类键盘/头显的简化数据同级）
#   3 = 再加全身骨骼与 ARKit52 表情（Agent 自己做 IK；关节表见 PRESENCE_BONES）
PRESENCE_LEVELS = {
    1: {"hands": False, "bones": False},
    2: {"hands": True, "bones": False},
    3: {"hands": True, "bones": True},
}


# 关节表（**顺序即二进制帧里的关节序号**）：VRM 1.0 humanoid 标准骨骼，与 three-vrm 的
# 枚举顺序一致（从 vendored three-vrm 抽出）。渲染端用同一张表把序号还原成骨头，
# 两边顺序必须完全一致——改动这里就要同步 static/xr/xr-avatars.js 的 PRESENCE_BONES。
PRESENCE_BONES = (
    "hips", "spine", "chest", "upperChest", "neck", "head", "leftEye", "rightEye", "jaw",
    "leftUpperLeg", "leftLowerLeg", "leftFoot", "leftToes",
    "rightUpperLeg", "rightLowerLeg", "rightFoot", "rightToes",
    "leftShoulder", "leftUpperArm", "leftLowerArm", "leftHand",
    "rightShoulder", "rightUpperArm", "rightLowerArm", "rightHand",
    "leftThumbMetacarpal", "leftThumbProximal", "leftThumbDistal",
    "leftIndexProximal", "leftIndexIntermediate", "leftIndexDistal",
    "leftMiddleProximal", "leftMiddleIntermediate", "leftMiddleDistal",
    "leftRingProximal", "leftRingIntermediate", "leftRingDistal",
    "leftLittleProximal", "leftLittleIntermediate", "leftLittleDistal",
    "rightThumbMetacarpal", "rightThumbProximal", "rightThumbDistal",
    "rightIndexProximal", "rightIndexIntermediate", "rightIndexDistal",
    "rightMiddleProximal", "rightMiddleIntermediate", "rightMiddleDistal",
    "rightRingProximal", "rightRingIntermediate", "rightRingDistal",
    "rightLittleProximal", "rightLittleIntermediate", "rightLittleDistal",
)
PRESENCE_BONE_INDEX = {name: i for i, name in enumerate(PRESENCE_BONES)}

# ARKit 52 表情表（**顺序即帧里的字节序**）：与 static/xr/xr-avatars.js 的 ARKIT52 必须逐字一致。
PRESENCE_FACE = (
    "eyeBlinkLeft", "eyeBlinkRight", "eyeLookDownLeft", "eyeLookDownRight",
    "eyeLookInLeft", "eyeLookInRight", "eyeLookOutLeft", "eyeLookOutRight",
    "eyeLookUpLeft", "eyeLookUpRight", "eyeSquintLeft", "eyeSquintRight",
    "eyeWideLeft", "eyeWideRight", "browDownLeft", "browDownRight", "browInnerUp",
    "browOuterUpLeft", "browOuterUpRight", "noseSneerLeft", "noseSneerRight",
    "cheekPuff", "cheekSquintLeft", "cheekSquintRight", "jawOpen", "jawLeft",
    "jawRight", "jawForward", "mouthLeft", "mouthRight", "mouthFrownLeft",
    "mouthFrownRight", "mouthSmileLeft", "mouthSmileRight", "mouthDimpleLeft",
    "mouthDimpleRight", "mouthPucker", "mouthStretchLeft", "mouthStretchRight",
    "mouthPressLeft", "mouthPressRight", "mouthRollLower", "mouthRollUpper",
    "mouthShrugLower", "mouthShrugUpper", "mouthClose", "mouthFunnel",
    "mouthLowerDownLeft", "mouthLowerDownRight", "mouthUpperUpLeft",
    "mouthUpperUpRight", "tongueOut",
)
P_DIRTY_BONES = 16
# 64 = 一次性骨骼动画命令（关键帧序列）；动画只在近距离 LOD 下发。
P_DIRTY_FACE = 32
P_DIRTY_ANIMATION = 64
PRESENCE_FACE_SET = frozenset(PRESENCE_FACE)
PRESENCE_FACE_INDEX = {name: i for i, name in enumerate(PRESENCE_FACE)}


def _presence_hands_dirty(ph, ch) -> bool:
    """手部是否变化：数量不同算变；任一分量超阈值也算变（避免每帧都发手）。"""
    ph, ch = ph or [], ch or []
    if len(ph) != len(ch):
        return True
    for a, b in zip(ph, ch):
        if a.get("handedness") != b.get("handedness"):
            return True
        for key in ("p", "q"):
            va, vb = (a.get(key) or []), (b.get(key) or [])
            for i in range(min(len(va), len(vb))):
                if abs(va[i] - vb[i]) > PRESENCE_HANDS_EPS:
                    return True
    return False


def _presence_dirty(prev: dict | None, cur: dict) -> int:
    """对比上一次位姿得出脏位掩码；没有上一次（首次/全量重来）视为全脏。

    注意这里必须包含 BONES/FACE：漏了它们的话，Agent 的**首次**骨骼上报不会被标记为脏，
    而后续上报骨骼没变也不算脏 ⇒ 骨骼永远发不出去（静默失效）。
    """
    if not prev:
        return (P_DIRTY_POS | P_DIRTY_ORIENT | P_DIRTY_HANDS | P_DIRTY_STATE
                | P_DIRTY_BONES | P_DIRTY_FACE | P_DIRTY_ANIMATION)
    bits = 0
    pp, cp = (prev.get("p") or [0.0, 0.0, 0.0]), (cur.get("p") or [0.0, 0.0, 0.0])
    if any(abs(cp[i] - pp[i]) > PRESENCE_POS_EPS for i in range(3)):
        bits |= P_DIRTY_POS
    if (abs(cur.get("yaw", 0.0) - prev.get("yaw", 0.0)) > PRESENCE_ANGLE_EPS
            or abs(cur.get("pitch", 0.0) - prev.get("pitch", 0.0)) > PRESENCE_ANGLE_EPS):
        bits |= P_DIRTY_ORIENT
    if _presence_hands_dirty(prev.get("hands"), cur.get("hands")):
        bits |= P_DIRTY_HANDS
    if (prev.get("bones") or {}) != (cur.get("bones") or {}):
        bits |= P_DIRTY_BONES
    if (prev.get("face") or {}) != (cur.get("face") or {}):
        bits |= P_DIRTY_FACE
    if json.dumps(prev.get("state"), sort_keys=True) != json.dumps(cur.get("state"), sort_keys=True):
        bits |= P_DIRTY_STATE
    # animation 是一次性命令：即使两个命令内容相同，也必须各自发出去，不能被
    # 100ms presence 合并窗口吞掉（例如 replace 后紧接 stop）。
    if "animation" in cur:
        bits |= P_DIRTY_ANIMATION
    return bits


def _presence_lod_bits(dist: float) -> int:
    """按距离给出这一帧允许携带的字段。骨骼与表情最贵，只给近处。"""
    if dist < PRESENCE_LOD_NEAR_M:
        return (P_DIRTY_POS | P_DIRTY_ORIENT | P_DIRTY_HANDS | P_DIRTY_STATE
                | P_DIRTY_BONES | P_DIRTY_FACE | P_DIRTY_ANIMATION)
    if dist < PRESENCE_LOD_MID_M:
        return P_DIRTY_POS | P_DIRTY_ORIENT
    return P_DIRTY_POS


def _q16_pos(v: float) -> int:
    return max(-32768, min(32767, int(round(v * PRESENCE_PACK_POS))))


def _q16_angle(a: float) -> int:
    """角度量化到 int16（满量程 2π），并绕回 int16 range 避免跨周跳变。"""
    return ((int(round(a * PRESENCE_PACK_ANGLE)) + 32768) % 65536) - 32768


def _presence_entry_bytes(user_id: int, kind: int, mask: int, pose: dict | None) -> bytes:
    """一个成员的一条增量：u32 用户 id + u8 kind（0=位姿 1=离开）+ u8 脏位 + 字段。

    kind 单独占一字节而不是拿 mask=0 表示离开——否则「这一帧什么都没变」与「人走了」
    无法区分。
    """
    out = bytearray()
    out += struct.pack("<IBB", user_id, kind, mask & 0xFF)
    if kind != 0 or not pose:
        return bytes(out)
    if mask & P_DIRTY_POS:
        p = pose.get("p") or [0.0, 0.0, 0.0]
        out += struct.pack("<3h", _q16_pos(p[0]), _q16_pos(p[1]), _q16_pos(p[2]))
    if mask & P_DIRTY_ORIENT:
        out += struct.pack("<2h", _q16_angle(pose.get("yaw", 0.0)), _q16_angle(pose.get("pitch", 0.0)))
    if mask & P_DIRTY_HANDS:
        hands = (pose.get("hands") or [])[:PRESENCE_MAX_HANDS]
        out += struct.pack("<B", len(hands))
        for h in hands:
            # 0=未知/Agent 旧客户端，1=左手，2=右手；用于 XR 人类 Avatar IK。
            side = {"left": 1, "right": 2}.get(h.get("handedness"), 0)
            out += struct.pack("<B", side)
            hp = h.get("p") or [0.0, 0.0, 0.0]
            out += struct.pack("<3h", _q16_pos(hp[0]), _q16_pos(hp[1]), _q16_pos(hp[2]))
            q = (h.get("q") or [0.0, 0.0, 0.0, 1.0])[:4]
            out += struct.pack("<4h", *[max(-32768, min(32767, int(round(v * 32767)))) for v in q])
    if mask & P_DIRTY_STATE:
        raw = json.dumps(pose.get("state"), ensure_ascii=False, separators=(",", ":")).encode("utf-8")[:255]
        out += struct.pack("<B", len(raw)) + raw
    if mask & P_DIRTY_BONES:
        # u8 根数 + 每根 (u8 关节序号 + 4×int16 四元数) ≈ 9B/根；没报的关节渲染端保持程序化动画
        items = [(PRESENCE_BONE_INDEX[n], q) for n, q in (pose.get("bones") or {}).items()
                 if n in PRESENCE_BONE_INDEX]
        items = items[:len(PRESENCE_BONES)]
        out += struct.pack("<B", len(items))
        for idx, q in items:
            out += struct.pack("<B", idx)
            out += struct.pack("<4h", *[max(-32768, min(32767, int(round(float(v) * 32767)))) for v in list(q)[:4]])
    if mask & P_DIRTY_FACE:
        # u8 个数 + 每个 (u8 表情序号 + u8 权重)：只发变化的，未报的渲染端保持
        items = [(PRESENCE_FACE_INDEX[n], w) for n, w in (pose.get("face") or {}).items()
                 if n in PRESENCE_FACE_INDEX]
        out += struct.pack("<B", len(items))
        for idx, w in items:
            out += struct.pack("<2B", idx, max(0, min(255, int(round(float(w) * 255)))))
    if mask & P_DIRTY_ANIMATION:
        # 动画是一次性命令，长度用 u32；服务端已限制总大小，不能被当成无限流。
        raw = json.dumps(pose.get("animation"), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        out += struct.pack("<I", len(raw)) + raw
    return bytes(out)


def _presence_frame_bytes(entries) -> bytes:
    """整帧：magic + version + 成员数，随后逐条成员增量。"""
    out = bytearray()
    out += struct.pack("<BBB", PRESENCE_BIN_MAGIC, PRESENCE_BIN_VERSION, min(len(entries), 255))
    for user_id, kind, mask, pose in entries[:PRESENCE_MAX_ENTRIES]:
        out += _presence_entry_bytes(user_id, kind, mask, pose)
    return bytes(out)


class PresenceHand(BaseModel):
    p: list[float]
    q: list[float] | None = None
    handedness: Literal["left", "right"] | None = None


class PresenceAnimation(BaseModel):
    """一个可排队的 Avatar 关键帧序列命令。

    action=replace：立即终止当前序列并播放本序列；
    action=append：当前序列播完后追加本序列；
    action=stop：清空当前与排队序列（不需要 keyframes）。
    """

    action: Literal["replace", "append", "stop"] = "replace"
    id: str | None = None
    loop: bool = False
    keyframes: list[dict[str, Any]] | None = None


class PresenceUpdate(BaseModel):
    p: list[float]                     # 头/身体位置（世界坐标，米）
    yaw: float = 0.0                   # 朝向（弧度，绕 y）
    pitch: float = 0.0
    hands: list[PresenceHand] | None = None   # 可选：双手 6DoF（头显才有）
    state: dict | None = None          # 可选小状态（speaking/expression/…）
    # Agent 自报的能力档（见 PRESENCE_LEVELS）。不传则按载荷推断：有 hands 记 2，否则 1。
    level: int | None = Field(default=None, ge=1, le=3)
    # level 3 才有：关节名（VRM humanoid 标准名）→ 四元数 [x,y,z,w]；未报的关节仍走渲染端的程序化动画
    bones: dict[str, list[float]] | None = None
    # level 3 才有：ARKit52 表情名 → 权重 0..1（只报变化的，没报的保持）
    face: dict[str, float] | None = None
    # level 3 才有：一次性关键帧动画命令；渲染端本地按时间轴播放，不要求 Agent 逐拍重发。
    animation: PresenceAnimation | None = None


def _presence_secs(conn, a: str | None, b: str | None) -> float:
    """两个时间戳字符串之差（秒，时间运算交给 SQLite，与仓库其它处一致）。

    任一侧为空/非法返回极大值（视为「太久远」，让调用方走全量重置）。
    """
    if not a or not b:
        return 1e9
    row = conn.execute("SELECT (julianday(?) - julianday(?)) * 86400.0 AS s", (b, a)).fetchone()
    if not row or row["s"] is None:
        return 1e9
    return float(row["s"])


def _presence_level(body: PresenceUpdate) -> int:
    """解析并校验能力档：不传就按载荷推断（有 hands 记 2，否则 1）；超出该档直接 400。

    宁可明确报错也不静默丢字段——Agent 声明了 level 1 却发 hands，多半是它自己搞错了档位。

    **必须定义在 PresenceUpdate 之后**：Python 3.11 在函数定义时就会求值注解，
    前向引用会直接 NameError（3.14 惰性求值所以本地测不出来，生产是 3.11）。
    """
    hands = body.hands or []
    level = body.level if body.level is not None else (2 if hands else 1)
    if hands and not PRESENCE_LEVELS[level]["hands"]:
        raise HTTPException(
            status_code=400,
            detail=f"level {level} 只报位姿，不能带 hands（要上报双手请声明 level 2）",
        )
    wants_bones = bool(body.bones) or bool(body.face) or body.animation is not None
    if wants_bones and not PRESENCE_LEVELS[level]["bones"]:
        raise HTTPException(
            status_code=400,
            detail=f"level {level} 不支持骨骼/表情/动画上报（要上报请声明 level 3）",
        )
    return level


def _presence_bones_validate(raw: Any, path: str = "bones") -> dict[str, list[float]]:
    """校验并归一化一组 VRM humanoid 四元数。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail=f"{path} 必须是对象")
    bones: dict[str, list[float]] = {}
    for name, q in raw.items():
        if name not in PRESENCE_BONE_INDEX:
            raise HTTPException(status_code=400, detail=f"未知关节 {name}（须是 VRM humanoid 标准骨骼名）")
        if not isinstance(q, (list, tuple)) or len(q) != 4:
            raise HTTPException(status_code=400, detail=f"{path}.{name} 的四元数必须是 [x, y, z, w]")
        try:
            vals = [float(v) for v in q]
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"{path}.{name} 的四元数必须是数字") from exc
        if not all(math.isfinite(v) for v in vals):
            raise HTTPException(status_code=400, detail=f"{path}.{name} 的四元数不能含 NaN/Infinity")
        norm = math.sqrt(sum(v * v for v in vals))
        if norm < 1e-6:
            raise HTTPException(status_code=400, detail=f"{path}.{name} 的四元数长度为零")
        bones[name] = [v / norm for v in vals]
    return bones


def _presence_face_validate(raw: Any, path: str = "face") -> dict[str, float]:
    """校验并裁剪一组 ARKit52 表情权重。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail=f"{path} 必须是对象")
    face: dict[str, float] = {}
    for name, w in raw.items():
        if name not in PRESENCE_FACE_SET:
            raise HTTPException(status_code=400, detail=f"未知表情 {name}（须是 ARKit52 名称）")
        try:
            wf = float(w)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"{path}.{name} 的权重必须是数字") from exc
        if not math.isfinite(wf):
            raise HTTPException(status_code=400, detail=f"{path}.{name} 的权重不能是 NaN/Infinity")
        face[name] = min(1.0, max(0.0, wf))
    return face


def _presence_animation_validate(raw: PresenceAnimation | None) -> dict | None:
    """把动画命令规范化为可安全进入 presence 增量日志的 JSON。"""
    if raw is None:
        return None
    action = raw.action
    if raw.id is not None:
        if not isinstance(raw.id, str) or not raw.id.strip():
            raise HTTPException(status_code=400, detail="animation.id 不能为空")
        if len(raw.id) > PRESENCE_ANIMATION_ID_CHARS:
            raise HTTPException(status_code=400, detail=f"animation.id 过长（上限 {PRESENCE_ANIMATION_ID_CHARS} 字符）")
        animation_id = raw.id.strip()
    else:
        animation_id = None

    if action == "stop":
        if raw.keyframes:
            raise HTTPException(status_code=400, detail="animation=stop 不能带 keyframes")
        return {"action": "stop", **({"id": animation_id} if animation_id else {})}

    frames = raw.keyframes or []
    if not frames:
        raise HTTPException(status_code=400, detail="animation.replace/append 必须至少包含一个 keyframe")
    if len(frames) > PRESENCE_ANIMATION_MAX_KEYFRAMES:
        raise HTTPException(
            status_code=413,
            detail=f"animation.keyframes 过多（上限 {PRESENCE_ANIMATION_MAX_KEYFRAMES} 帧）",
        )

    out_frames = []
    prev_t = -1.0
    for i, frame in enumerate(frames):
        if not isinstance(frame, dict):
            raise HTTPException(status_code=400, detail=f"animation.keyframes[{i}] 必须是对象")
        try:
            t = float(frame.get("t"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"animation.keyframes[{i}].t 必须是数字") from exc
        if not math.isfinite(t) or t < 0:
            raise HTTPException(status_code=400, detail=f"animation.keyframes[{i}].t 必须是非负有限数字")
        if t <= prev_t:
            raise HTTPException(status_code=400, detail="animation.keyframes 的 t 必须严格递增")
        if t > PRESENCE_ANIMATION_MAX_DURATION:
            raise HTTPException(
                status_code=400,
                detail=f"animation.keyframes[{i}].t 超出最大时长 {PRESENCE_ANIMATION_MAX_DURATION:g}s",
            )
        prev_t = t
        bones = _presence_bones_validate(frame.get("bones"), f"animation.keyframes[{i}].bones")
        face = _presence_face_validate(frame.get("face"), f"animation.keyframes[{i}].face")
        if not bones and not face:
            raise HTTPException(status_code=400, detail=f"animation.keyframes[{i}] 至少要有 bones 或 face")
        item: dict[str, Any] = {"t": t}
        if bones:
            item["bones"] = bones
        if face:
            item["face"] = face
        out_frames.append(item)

    out: dict[str, Any] = {
        "action": action,
        "loop": bool(raw.loop),
        "keyframes": out_frames,
    }
    if animation_id:
        out["id"] = animation_id
    encoded = json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > PRESENCE_ANIMATION_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"animation 过大（上限 {PRESENCE_ANIMATION_MAX_BYTES} 字节）",
        )
    return out


def _presence_validate(body: PresenceUpdate) -> dict:
    """位姿规范化 + 合法性校验（越界/NaN 直接 400）。"""
    p = _validate_pose_vec(body.p, "p")
    if abs(p[0]) > PRESENCE_RADIUS_LIMIT or abs(p[2]) > PRESENCE_RADIUS_LIMIT:
        raise HTTPException(status_code=400, detail=f"位置超出允许范围（±{PRESENCE_RADIUS_LIMIT:g}m）")
    if not (PRESENCE_Y_MIN <= p[1] <= PRESENCE_Y_MAX):
        raise HTTPException(status_code=400, detail=f"高度超出允许范围（{PRESENCE_Y_MIN:g}~{PRESENCE_Y_MAX:g}m）")
    for label, val in (("yaw", body.yaw), ("pitch", body.pitch)):
        if not math.isfinite(float(val)):
            raise HTTPException(status_code=400, detail=f"{label} 必须是有限数字")
    hands = []
    for i, h in enumerate(body.hands or []):
        if i >= PRESENCE_MAX_HANDS:
            raise HTTPException(status_code=400, detail=f"手部最多 {PRESENCE_MAX_HANDS} 个")
        hp = _validate_pose_vec(h.p, f"hands[{i}].p")
        hq = None
        if h.q is not None:
            if len(h.q) != 4:
                raise HTTPException(status_code=400, detail=f"hands[{i}].q 必须是四元数 [x, y, z, w]")
            hq = [float(v) for v in h.q]
            if not all(math.isfinite(v) for v in hq):
                raise HTTPException(status_code=400, detail=f"hands[{i}].q 不能包含 NaN 或 Infinity")
        hands.append({"p": hp, "q": hq, "handedness": h.handedness})
    state = body.state
    if state is not None:
        if not isinstance(state, dict):
            raise HTTPException(status_code=400, detail="state 必须是对象")
        if len(json.dumps(state, ensure_ascii=False)) > PRESENCE_STATE_CHARS:
            raise HTTPException(status_code=413, detail=f"state 过大（上限 {PRESENCE_STATE_CHARS} 字符）")
    bones = _presence_bones_validate(body.bones)
    face = _presence_face_validate(body.face)
    animation = _presence_animation_validate(body.animation)
    out = {
        "p": p, "yaw": float(body.yaw), "pitch": float(body.pitch),
        "hands": hands, "state": state, "bones": bones, "face": face,
    }
    if animation is not None:
        out["animation"] = animation
    return out


_presence_purge_at = 0.0


def _presence_purge(conn) -> None:
    """增量表滚动清理（走 idx_room_presence_log_time）。

    10Hz 上报下「每次写都清一次」会变成每秒上百次 DELETE，故节流到最多每 5 秒一次。
    清理是滚动的，晚几秒不影响正确性——超窗的游标本来就一律让客户端全量重取。
    """
    global _presence_purge_at
    now = time.monotonic()
    if now - _presence_purge_at < 5.0:
        return
    _presence_purge_at = now
    conn.execute("DELETE FROM room_presence_log WHERE created_at < datetime('now', ?)", (PRESENCE_RETENTION,))


@app.post("/api/rooms/{room_name}/presence")
def update_presence(room_name: str, body: PresenceUpdate, user: CurrentUser):
    """上报自己的 3D 位姿（头/身体 + 可选双手 6DoF）。

    全量表更新为该用户最新位姿；增量表按时间追加一条事件（同人 PRESENCE_COALESCE_MS 内合并）。
    **自然运动兜底**：与上一次位姿的水平位移超过「允许速度 × 间隔 + 容差」时，按上限
    **裁剪**（不是拒绝——硬拒会让滚轮快走的人类永久卡住）；别人永远看不到瞬移，
    响应带 `clamped: true` 与原位姿，Agent 据此按步行速度改正。
    """
    pose = _presence_validate(body)
    level = _presence_level(body)
    animation_sent = body.animation is not None
    with get_db() as conn:
        room, _member = _require_membership(conn, room_name, user["id"])
        now = _db_now(conn)
        prev = conn.execute(
            "SELECT pose, updated_at FROM room_presence WHERE room_id = ? AND user_id = ?",
            (room["id"], user["id"]),
        ).fetchone()
        prev_pose = None
        if prev and prev["pose"]:
            try:
                prev_pose = json.loads(prev["pose"])
            except ValueError:
                prev_pose = None
        clamped = False
        if prev_pose:
            # 自然运动：水平位移按「允许速度 × 间隔 + 容差」裁剪，而不是拒绝——人类滚轮
            # 走动/手柄传送若被硬拒会永久卡住；裁剪后别人永远看不到瞬移，Agent 也快不起来。
            # 响应里的 clamped=true 让 Agent 自查并按步行速度改正。
            dt = _presence_secs(conn, prev["updated_at"], now)
            dx = pose["p"][0] - prev_pose["p"][0]
            dz = pose["p"][2] - prev_pose["p"][2]
            dist = math.hypot(dx, dz)
            allowed = PRESENCE_WALK_SPEED * dt + PRESENCE_SPEED_SLACK
            if dist > allowed and dist > 0:
                k = allowed / dist
                pose["p"][0] = prev_pose["p"][0] + dx * k
                pose["p"][2] = prev_pose["p"][2] + dz * k
                clamped = True
        # 动画命令是控制事件，不受普通位姿的 100ms 合并窗口影响；否则 replace→stop
        # 或连续 append 可能在同一窗口内被吞掉，渲染端就无法按 Agent 的控制意图执行。
        log_it = animation_sent
        if prev and not log_it:
            log_it = _presence_secs(conn, prev["updated_at"], now) * 1000.0 >= PRESENCE_COALESCE_MS
        pose_json = json.dumps(pose, ensure_ascii=False)
        state_json = json.dumps(pose["state"], ensure_ascii=False) if pose["state"] is not None else None
        # 脏位：这次相比上次动了哪些字段。静止的人（位置/朝向/手/状态都没变）在增量帧里
        # 一个字节都不占，这是二进制帧省带宽的主要来源。
        dirty = _presence_dirty(prev_pose, pose)
        conn.execute(
            """
            INSERT INTO room_presence (room_id, user_id, pose, state, level, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(room_id, user_id) DO UPDATE SET
                pose = excluded.pose, state = excluded.state, level = excluded.level,
                updated_at = excluded.updated_at
            """,
            (room["id"], user["id"], pose_json, state_json, level, now),
        )
        if log_it:
            conn.execute(
                """
                INSERT INTO room_presence_log (room_id, user_id, kind, pose, state, dirty, created_at)
                VALUES (?, ?, 'pose', ?, ?, ?, ?)
                """,
                (room["id"], user["id"], pose_json, state_json, dirty, now),
            )
            _presence_purge(conn)
    # 注意：这里**不** notify_room——增量端点用服务端节流（见 presence_delta 的 hold），
    # 靠事件唤醒会让 10Hz × N 人的写入把等待者唤醒得比轮询还频繁（惊群）。
    return {"ok": True, "serverTime": now, "logged": log_it, "clamped": clamped, "pose": pose, "level": level, "dirty": dirty}


@app.post("/api/rooms/{room_name}/presence/leave")
def leave_presence(room_name: str, user: CurrentUser):
    """离开 3D：删除自己的最新位姿并追加一条 leave 事件，其他人立即移除其形象。"""
    with get_db() as conn:
        room, _member = _require_membership(conn, room_name, user["id"])
        now = _db_now(conn)
        conn.execute("DELETE FROM room_presence WHERE room_id = ? AND user_id = ?", (room["id"], user["id"]))
        conn.execute(
            "INSERT INTO room_presence_log (room_id, user_id, kind, created_at) VALUES (?, ?, 'leave', ?)",
            (room["id"], user["id"], now),
        )
        _presence_purge(conn)
    return {"ok": True, "serverTime": now}


@app.get("/api/rooms/{room_name}/presence")
def presence_snapshot(room_name: str, user: CurrentUser):
    """全量快照：房内在线的每人最新位姿 + 服务端当前时间（客户端把它当增量游标）。"""
    with get_db() as conn:
        room, _member = _require_membership(conn, room_name, user["id"])
        now = _db_now(conn)
        # 增量日志的当前最大 id：客户端拿它当 id 游标（比时间戳稳，见 presence_delta）
        log_id = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM room_presence_log WHERE room_id = ?", (room["id"],)
        ).fetchone()["m"]
        rows = conn.execute(
            """
            SELECT u.username, p.pose, p.state, p.level, p.updated_at
            FROM room_presence p
            JOIN users u ON u.id = p.user_id
            JOIN room_members m ON m.room_id = p.room_id AND m.user_id = p.user_id
            WHERE p.room_id = ? AND m.membership_status = 'active'
              AND m.last_seen_at > datetime('now', ?)
              -- 被封禁者立即从 3D 在场快照消失（与在线列表同一过滤）
              AND NOT EXISTS (
                  SELECT 1 FROM room_bans b
                  WHERE b.room_id = p.room_id AND b.user_id = p.user_id
                    AND (b.expires_at IS NULL OR b.expires_at > strftime('%Y-%m-%d %H:%M:%f', 'now'))
              )
            ORDER BY u.username
            """,
            (room["id"], ONLINE_WINDOW),
        ).fetchall()
    users = [
        {
            "username": r["username"],
            "pose": json.loads(r["pose"]) if r["pose"] else None,
            "state": json.loads(r["state"]) if r["state"] else None,
            # 能力档：老数据没有该列时按 2 处理（与推断规则一致）
            "level": int(r["level"] or 2),
            "at": r["updated_at"],
        }
        for r in rows
    ]
    return {"roomName": room["name"], "serverTime": now, "logId": log_id, "users": users}


@app.get("/api/rooms/{room_name}/presence/delta")
async def presence_delta(
    room_name: str,
    user: CurrentUser,
    since: str | None = Query(default=None),
    since_id: Annotated[int | None, Query(alias="sinceId", ge=0)] = None,
    hold: Annotated[int, Query(ge=0, le=500)] = 0,
    fmt: Annotated[str, Query(pattern="^(json|bin)$")] = "json",
):
    """增量：since（服务端时间戳）或 sinceId（增量日志的单调 id）之后的变更事件。

    **优先用 sinceId**：时间戳游标有毫秒级碰撞——与游标同一毫秒写入的事件因为
    `created_at > since` 不成立会被永久漏掉（10Hz 下撞毫秒很常见）；日志 id 单调递增，
    没有这个问题，而且比较更便宜。

    `hold`（毫秒）是**服务端节流**：不足 hold 毫秒就先等满再返回。客户端「返回就再发一次」
    即可拿到稳定 tick（10Hz 传 100），而服务端每 tick 只查一次库——这比「谁写入就唤醒谁」
    的事件长轮询更稳：10Hz × N 人的写入会把等待者唤醒到比纯轮询还频繁（惊群）。

    `fmt=bin` 返回二进制帧（脏位增量 + 按到请求者的距离分级），游标在 `X-Presence-Id`
    （与 `X-Presence-Cursor`）响应头、`X-Presence-Reset` 表示需要重拉全量；
    不带 fmt 时保持原 JSON 形状不变（Agent/调试仍可用）。
    """
    if hold:
        await asyncio.sleep(min(hold, 500) / 1000.0)
    use_id = since_id is not None
    with get_db() as conn:
        room, _member = _require_membership(conn, room_name, user["id"])
        now = _db_now(conn)
        room_id, room_label = room["id"], room["name"]
        max_id = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM room_presence_log WHERE room_id = ?", (room_id,)
        ).fetchone()["m"]
        reset = False
        if not use_id:
            if not since:
                reset = True
            else:
                age = _presence_secs(conn, since, now)
                if age > PRESENCE_RETENTION_SECS or age < -5.0:
                    reset = True
        rows = []
        requester_pose = None
        if not reset:
            if use_id:
                rows = conn.execute(
                    """
                    SELECT l.id, l.user_id, u.username, l.kind, l.pose, l.state, l.dirty, l.created_at
                    FROM room_presence_log l
                    JOIN users u ON u.id = l.user_id
                    WHERE l.room_id = ? AND l.id > ? AND l.user_id != ?
                      AND (
                          l.kind = 'leave'
                          OR (EXISTS (
                              SELECT 1 FROM room_members m
                              WHERE m.room_id = l.room_id AND m.user_id = l.user_id
                                AND m.membership_status = 'active'
                          ) AND NOT EXISTS (
                              SELECT 1 FROM room_bans b
                              WHERE b.room_id = l.room_id AND b.user_id = l.user_id
                                AND (b.expires_at IS NULL OR b.expires_at > strftime('%Y-%m-%d %H:%M:%f', 'now'))
                          ))
                      )
                    ORDER BY l.id ASC
                    LIMIT ?
                    """,
                    (room_id, since_id, user["id"], PRESENCE_DELTA_LIMIT),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT l.id, l.user_id, u.username, l.kind, l.pose, l.state, l.dirty, l.created_at
                    FROM room_presence_log l
                    JOIN users u ON u.id = l.user_id
                    WHERE l.room_id = ? AND l.created_at > ? AND l.user_id != ?
                      AND (
                          l.kind = 'leave'
                          OR (EXISTS (
                              SELECT 1 FROM room_members m
                              WHERE m.room_id = l.room_id AND m.user_id = l.user_id
                                AND m.membership_status = 'active'
                          ) AND NOT EXISTS (
                              SELECT 1 FROM room_bans b
                              WHERE b.room_id = l.room_id AND b.user_id = l.user_id
                                AND (b.expires_at IS NULL OR b.expires_at > strftime('%Y-%m-%d %H:%M:%f', 'now'))
                          ))
                      )
                    ORDER BY l.id ASC
                    LIMIT ?
                    """,
                    (room_id, since, user["id"], PRESENCE_DELTA_LIMIT),
                ).fetchall()
            if len(rows) >= PRESENCE_DELTA_LIMIT:
                reset = True      # 落后太多：直接全量重取，避免分页追赶
                rows = []
            # 请求者自己的最新位姿：服务端据此给每个成员定细节档（近/中/远）
            me = conn.execute(
                "SELECT pose FROM room_presence WHERE room_id = ? AND user_id = ?",
                (room_id, user["id"]),
            ).fetchone()
            if me and me["pose"]:
                try:
                    requester_pose = json.loads(me["pose"])
                except ValueError:
                    requester_pose = None
    if fmt == "bin":
        entries = []
        for r in rows:
            if r["kind"] != "pose" or not r["pose"]:
                entries.append((r["user_id"], 1, 0, None))     # kind=1：离开
                continue
            try:
                pose = json.loads(r["pose"])
            except ValueError:
                continue
            mask = int(r["dirty"] or 0)
            if requester_pose:
                rp = requester_pose.get("p") or []
                mp = pose.get("p") or []
                if len(rp) >= 3 and len(mp) >= 3:
                    mask &= _presence_lod_bits(math.hypot(mp[0] - rp[0], mp[2] - rp[2]))
            if not mask:
                continue        # 远处且什么都没变：这一帧不占字节
            entries.append((r["user_id"], 0, mask, pose))
        return Response(
            content=_presence_frame_bytes(entries),
            media_type="application/octet-stream",
            headers={
                # 下一轮的游标：有行就用最后一行（若中途还有更新，宁可多收不可漏收）
                "X-Presence-Id": str(rows[-1]["id"] if rows else max_id),
                "X-Presence-Cursor": now,
                "X-Presence-Reset": "1" if reset else "0",
                "Cache-Control": "no-store",
            },
        )
    events = [
        {
            "username": r["username"],
            "kind": r["kind"],
            "pose": json.loads(r["pose"]) if r["pose"] else None,
            "state": json.loads(r["state"]) if r["state"] else None,
            "at": r["created_at"],
        }
        for r in rows
    ]
    return {"roomName": room_label, "serverTime": now, "reset": reset, "events": events}


@app.get("/api/archives/{room_id}/files")
def list_archive_files(room_id: int, user: CurrentUser):
    """归档房间的共同文件列表（只读）。"""
    with get_db() as conn:
        room, _member = _require_archive_access(conn, room_id, user)
        rows = conn.execute(
            FILE_SELECT + " WHERE f.room_id = ? ORDER BY f.updated_at DESC, f.id ASC",
            (room["id"],),
        ).fetchall()
    return {
        "roomName": room["name"],
        "revision": room["files_revision"],
        "readOnly": True,
        "files": [_file_dict(row, room["name"], archive_id=room["id"]) for row in rows],
    }


@app.get("/api/archives/{room_id}/files/{file_id}/content")
def download_archive_file_content(room_id: int, file_id: int, user: CurrentUser, download: bool = False):
    with get_db() as conn:
        room, _member = _require_archive_access(conn, room_id, user)
        row = _get_file(conn, room["id"], file_id)
        if not row or not row["content_path"]:
            raise HTTPException(status_code=404, detail="文件不存在")
        path = FILES_DIR / row["content_path"]
        if not path.is_file():
            raise HTTPException(status_code=404, detail="文件内容缺失")
        mime = row["mime"]
        name = row["name"]
    return _file_response(path, name, inline=not download, media_type=mime)


# ---------- 页面与说明书 ----------

def _base_url(request: Request) -> str:
    """取请求的 origin（协议+主机+端口），供说明书替换 {{BASE_URL}} 占位符。

    反代（nginx）会转发 Host 与 X-Forwarded-Proto，因此这里拿到的就是
    用户实际访问的地址，不写死 127.0.0.1 或某个域名。
    """
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}"


# rules 文本里的 {{BASE_URL}} 占位符按请求来源填充（中间件记录，dict 组装处读取）。
_request_base_url: ContextVar[str] = ContextVar("request_base_url", default="")


@app.middleware("http")
async def _remember_base_url(request: Request, call_next):
    token = _request_base_url.set(_base_url(request))
    try:
        return await call_next(request)
    finally:
        _request_base_url.reset(token)


def _fill_base_url(text: str | None) -> str:
    if not text:
        return ""
    base = _request_base_url.get()
    return text.replace("{{BASE_URL}}", base) if base else text


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/skill.md")
def skill_doc(request: Request):
    text = SKILL_PATH.read_text(encoding="utf-8")
    return PlainTextResponse(text.replace("{{BASE_URL}}", _base_url(request)), media_type="text/markdown")


@app.get("/guide")
def human_guide(lang: str | None = None):
    if lang == "en":
        return FileResponse(STATIC_DIR / "guide.en.html")
    return FileResponse(STATIC_DIR / "guide.html")


@app.get("/guide.md")
def human_guide_md(request: Request, lang: str | None = None):
    path = GUIDE_EN_PATH if lang == "en" else GUIDE_PATH
    text = path.read_text(encoding="utf-8")
    return PlainTextResponse(text.replace("{{BASE_URL}}", _base_url(request)), media_type="text/markdown")


@app.get("/scripts/inbox.py")
def script_inbox():
    return PlainTextResponse((ROOT_DIR / "scripts" / "inbox.py").read_text(encoding="utf-8"), media_type="text/x-python")


@app.get("/scripts/watch.py")
def script_watch():
    return PlainTextResponse((ROOT_DIR / "scripts" / "watch.py").read_text(encoding="utf-8"), media_type="text/x-python")


install_message_events(
    app, get_db=get_db, require_user=require_user,
    require_membership=_require_membership, fetch_messages=_fetch_messages,
    room_event=_room_event, user_by_id=_user_by_id,
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
