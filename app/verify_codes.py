"""验证码签发与核验（手机 / 邮箱共用一套节流与消费规则）。

策略（对齐 clip 的 CopyToMe，参数见 app/config.py）：
  重发间隔 60s · 每目标每日 10 次 · 全站每日 300 次 · 有效期 300s · 每码最多试 5 次

手机码由阿里云生成并核验（本服务不存码）；邮箱码由本服务生成、只存 PBKDF2 哈希。
两条通道都用「核验通过即写 verified_at」做消费式防重放，且核验时用途必须一致，
避免注册用的验证结果被挪去绑定/重置密码。
"""

import hmac
import secrets
from datetime import datetime, timedelta, timezone

from . import auth, config, mailer, sms
from .db import get_db

_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class VerifyError(Exception):
    """验证码相关失败；status_code 直接作为 HTTP 状态码。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(moment: datetime) -> str:
    return moment.strftime(_TIME_FORMAT)


def _parse(text: str) -> datetime:
    return datetime.strptime(text, _TIME_FORMAT).replace(tzinfo=timezone.utc)


def day_key(moment: datetime | None = None) -> str:
    """发送当天的 UTC 日期，每日上限按它汇总。"""
    return (moment or _now()).strftime("%Y-%m-%d")


def generate_code() -> str:
    """6 位数字码，不带前导零（与 clip 的 GetInt32(100000, 1000000) 一致）。"""
    return str(secrets.randbelow(900_000) + 100_000)


def send(channel: str, target: str, purpose: str) -> dict:
    """签发并发送一个验证码。返回 {expiresIn, interval}，失败抛 VerifyError。"""
    if purpose not in config.PURPOSES:
        raise VerifyError(400, "未知的验证码用途")
    normalized = _require_target(channel, target)
    if channel == "phone" and not config.sms_available():
        raise VerifyError(400, "短信通道未启用")
    if channel == "email" and not config.email_available():
        raise VerifyError(400, "邮箱通道未启用")

    now = _now()
    today = day_key(now)
    with get_db() as conn:
        latest = conn.execute(
            "SELECT sent_at FROM verify_codes WHERE channel = ? AND target = ?"
            " ORDER BY id DESC LIMIT 1",
            (channel, normalized),
        ).fetchone()
        if latest:
            waited = (now - _parse(latest["sent_at"])).total_seconds()
            remaining = config.RESEND_INTERVAL_SECONDS - int(waited)
            if remaining > 0:
                raise VerifyError(429, f"发送太频繁，请 {remaining} 秒后再试")
        sent_today = conn.execute(
            "SELECT COUNT(*) AS n FROM verify_codes WHERE channel = ? AND target = ? AND day_key = ?",
            (channel, normalized, today),
        ).fetchone()["n"]
        if sent_today >= config.daily_send_cap():
            raise VerifyError(429, "今日验证码发送次数已达上限，请明天再试")
        total_today = conn.execute(
            "SELECT COUNT(*) AS n FROM verify_codes WHERE day_key = ?", (today,)
        ).fetchone()["n"]
        if total_today >= config.global_daily_send_cap():
            raise VerifyError(429, "今日验证码发送总量已达上限，请明天再试")

        code = generate_code() if channel == "email" else None
        # 覆盖语义（等价阿里云 DuplicatePolicy=1）：把该目标尚在有效期内的旧码作废。
        # 保留行不删——每日上限按行数汇总，删了就等于把额度还回去了。
        conn.execute(
            "UPDATE verify_codes SET expires_at = ?"
            " WHERE channel = ? AND target = ? AND expires_at > ?",
            (_fmt(now), channel, normalized, _fmt(now)),
        )
        conn.execute(
            "INSERT INTO verify_codes"
            " (channel, target, purpose, code_hash, sent_at, expires_at, day_key)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                channel,
                normalized,
                purpose,
                auth.hash_password(code) if code else None,
                _fmt(now),
                _fmt(now + timedelta(seconds=config.CODE_TTL_SECONDS)),
                today,
            ),
        )

    # 网络调用放在事务外：别让发短信/发邮件占着 SQLite 连接
    try:
        if channel == "phone":
            sms.send_code(normalized)
        else:
            mailer.send_code(normalized, code or "")
    except (sms.SmsError, mailer.MailError) as exc:
        # 发失败就撤掉这条记录，别白占今日额度
        with get_db() as conn:
            conn.execute(
                "DELETE FROM verify_codes WHERE channel = ? AND target = ? AND sent_at = ?",
                (channel, normalized, _fmt(now)),
            )
        raise VerifyError(400, str(exc)) from exc
    return {"expiresIn": config.CODE_TTL_SECONDS, "interval": config.RESEND_INTERVAL_SECONDS}


def consume(channel: str, target: str, purpose: str, code: str) -> str:
    """核验并消费一个验证码；成功返回归一化后的 target，失败抛 VerifyError。"""
    if purpose not in config.PURPOSES:
        raise VerifyError(400, "未知的验证码用途")
    normalized = _require_target(channel, target)
    submitted = (code or "").strip()
    if not submitted:
        raise VerifyError(400, "请填写验证码")

    now = _now()
    debug = config.sms_debug_code()
    failure: VerifyError | None = None
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, code_hash, expires_at, verified_at, attempts FROM verify_codes"
            " WHERE channel = ? AND target = ? AND purpose = ? ORDER BY id DESC LIMIT 1",
            (channel, normalized, purpose),
        ).fetchone()
        if (
            row is None
            or row["verified_at"]
            or _parse(row["expires_at"]) <= now
            or row["attempts"] >= config.MAX_VERIFY_ATTEMPTS
        ):
            raise VerifyError(400, "验证码不存在或已失效，请重新获取")

        if debug and hmac.compare_digest(submitted.encode(), debug.encode()):
            ok = True
        elif channel == "phone":
            try:
                ok = sms.check_code(normalized, submitted)
            except sms.SmsError as exc:
                # 无法判定（网络/配置问题）：不计尝试次数，也不消费
                raise VerifyError(400, str(exc)) from exc
        else:
            ok = auth.verify_password(submitted, row["code_hash"] or "")

        if not ok:
            used = row["attempts"] + 1
            conn.execute("UPDATE verify_codes SET attempts = ? WHERE id = ?", (used, row["id"]))
            left = config.MAX_VERIFY_ATTEMPTS - used
            failure = VerifyError(
                400,
                "验证码错误次数过多，请重新获取" if left <= 0 else f"验证码不正确，还可尝试 {left} 次",
            )
        else:
            # 消费式防重放：核验通过即标记，同一个码不能用第二次
            conn.execute(
                "UPDATE verify_codes SET verified_at = ? WHERE id = ? AND verified_at IS NULL",
                (_fmt(now), row["id"]),
            )
    if failure:
        raise failure
    return normalized


def _require_target(channel: str, target: str | None) -> str:
    if channel not in ("phone", "email"):
        raise VerifyError(400, "未知的验证码通道")
    normalized = config.normalize_target(channel, target)
    if not normalized:
        raise VerifyError(400, "手机号格式不正确" if channel == "phone" else "邮箱格式不正确")
    return normalized
