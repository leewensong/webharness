"""运行期配置：人类账号的手机短信 / 邮箱验证码通道。

全项目唯一读取环境变量的地方。凭证只走环境变量（或 systemd 的
EnvironmentFile=/etc/webharness/env），不落仓库、不写日志。

短信走阿里云「号码认证服务 PNVS」（验证码由阿里云生成与核验，本服务不存码）；
邮箱走 SMTP，验证码由本服务生成。两条通道都用「调试码」逃生口：
设了 WEBHARNESS_SMS_DEBUG_CODE 即视为两条通道都可用，且该字面量直接通过核验
（本地开发与 e2e 用；生产不设）。
"""

import os
import re

DEFAULT_SIGN_NAME = "恒创联众"
DEFAULT_TEMPLATE_CODE = "100001"
DEFAULT_SMTP_PORT = 587
DEFAULT_FROM_NAME = "WebHarness.Chat"
DEFAULT_DAILY_SEND_CAP = 10
DEFAULT_GLOBAL_DAILY_CAP = 300
# 每 IP 每分钟发码上限。比 clip 的 5 宽松：同一间房的人常从同一出口 IP 注册，
# 5 会让第 6 个人直接失败；真正的防滥用靠「每目标 60s 间隔 + 每日上限」。
DEFAULT_SEND_CODE_PER_MINUTE = 20

# 验证码策略（固定值，不走环境变量；改这里也要同步改模板文案里的 ${min}）
CODE_LENGTH = 6
CODE_TTL_SECONDS = 300
RESEND_INTERVAL_SECONDS = 60
MAX_VERIFY_ATTEMPTS = 5
# 发码用途：注册 / 登录 / 绑定换绑 / 忘记密码。核验时必须与发码时一致，防止串用。
PURPOSES = ("register", "login", "bind", "reset")

PHONE_PATTERN = re.compile(r"^1[3-9]\d{9}$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_EMAIL_LENGTH = 254

VERIFY_MODES = ("auto", "required", "off")


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_flag(name: str) -> bool | None:
    value = _env(name)
    if value is None:
        return None
    return value.lower() in ("1", "true", "yes", "on")


# ---------- 手机 / 邮箱规范化 ----------

def normalize_phone(text: str | None) -> str | None:
    """归一化为 11 位大陆手机号（去掉 +86 / 空格 / 连字符）；不合法返回 None。"""
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    if len(digits) == 13 and digits.startswith("86"):
        digits = digits[2:]
    return digits if PHONE_PATTERN.match(digits) else None


def normalize_email(text: str | None) -> str | None:
    """归一化为小写邮箱；不合法返回 None。"""
    if not text:
        return None
    email = text.strip().lower()
    if len(email) > MAX_EMAIL_LENGTH or not EMAIL_PATTERN.match(email):
        return None
    return email


def normalize_target(channel: str, text: str | None) -> str | None:
    if channel == "phone":
        return normalize_phone(text)
    if channel == "email":
        return normalize_email(text)
    return None


def mask_target(channel: str, target: str | None) -> str | None:
    """掩码展示用（138****7545 / a***@qq.com）。"""
    if not target:
        return None
    if channel == "phone":
        return f"{target[:3]}****{target[-4:]}" if len(target) >= 7 else "****"
    local, _, domain = target.partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"


# ---------- 短信（阿里云 PNVS）----------

def sms_debug_code() -> str | None:
    return _env("WEBHARNESS_SMS_DEBUG_CODE")


def sms_config() -> dict[str, str] | None:
    """真实阿里云凭证；未配齐返回 None（不含调试码逃生口）。"""
    ak_id = _env("WEBHARNESS_SMS_AK_ID")
    ak_secret = _env("WEBHARNESS_SMS_AK_SECRET")
    if not ak_id or not ak_secret:
        return None
    return {
        "ak_id": ak_id,
        "ak_secret": ak_secret,
        "sign_name": _env("WEBHARNESS_SMS_SIGN_NAME") or DEFAULT_SIGN_NAME,
        "template_code": _env("WEBHARNESS_SMS_TEMPLATE_CODE") or DEFAULT_TEMPLATE_CODE,
    }


def sms_available() -> bool:
    return sms_config() is not None or sms_debug_code() is not None


# ---------- 邮箱（SMTP）----------

def smtp_config() -> dict[str, object] | None:
    host = _env("WEBHARNESS_SMTP_HOST")
    username = _env("WEBHARNESS_SMTP_USERNAME")
    password = _env("WEBHARNESS_SMTP_PASSWORD")
    sender = _env("WEBHARNESS_SMTP_FROM") or username
    if not (host and username and password and sender):
        return None
    try:
        port = int(_env("WEBHARNESS_SMTP_PORT") or DEFAULT_SMTP_PORT)
    except ValueError:
        port = DEFAULT_SMTP_PORT
    ssl_flag = _env_flag("WEBHARNESS_SMTP_SSL")
    return {
        "host": host,
        "port": port,
        # 未显式指定时按惯例推断：465 走隐式 SSL，其余走 STARTTLS
        "ssl": port == 465 if ssl_flag is None else ssl_flag,
        "username": username,
        "password": password,
        "sender": sender,
        "from_name": _env("WEBHARNESS_SMTP_FROM_NAME") or DEFAULT_FROM_NAME,
    }


def email_available() -> bool:
    return smtp_config() is not None or sms_debug_code() is not None


# ---------- 注册/登录是否强制验证 ----------

def verify_mode() -> str:
    mode = (_env("WEBHARNESS_VERIFY_MODE") or "auto").lower()
    return mode if mode in VERIFY_MODES else "auto"


def any_channel_available() -> bool:
    return sms_available() or email_available()


def verify_required() -> bool:
    """注册与验证码登录是否强制要求验证码。"""
    mode = verify_mode()
    if mode == "off":
        return False
    if mode == "required":
        return True
    return any_channel_available()


def daily_send_cap() -> int:
    try:
        cap = int(_env("WEBHARNESS_SMS_DAILY_CAP") or DEFAULT_DAILY_SEND_CAP)
    except ValueError:
        cap = DEFAULT_DAILY_SEND_CAP
    return max(cap, 1)


def global_daily_send_cap() -> int:
    """全站每日发码上限（跨所有手机号/邮箱），兜底防短信轰炸。"""
    try:
        cap = int(_env("WEBHARNESS_SMS_GLOBAL_DAILY_CAP") or DEFAULT_GLOBAL_DAILY_CAP)
    except ValueError:
        cap = DEFAULT_GLOBAL_DAILY_CAP
    return max(cap, 1)


def send_code_per_minute() -> int:
    """每 IP 每分钟能请求几次发码。"""
    try:
        cap = int(_env("WEBHARNESS_SEND_CODE_PER_MIN") or DEFAULT_SEND_CODE_PER_MINUTE)
    except ValueError:
        cap = DEFAULT_SEND_CODE_PER_MINUTE
    return max(cap, 1)


def startup_warnings() -> list[str]:
    """启动时打印，避免环境变量拼错导致验证被静默降级。"""
    if verify_mode() == "required" and not any_channel_available():
        return [
            "WEBHARNESS_VERIFY_MODE=required 但短信与邮箱都未配置："
            "注册与验证码登录会返回 503。请检查 WEBHARNESS_SMS_AK_ID/…或 WEBHARNESS_SMTP_HOST/…"
        ]
    if verify_mode() == "auto" and not any_channel_available():
        return [
            "短信与邮箱都未配置（且未设 WEBHARNESS_SMS_DEBUG_CODE）："
            "注册将退回「用户名 + 密码」，验证码登录与绑定入口不显示。"
        ]
    return []
