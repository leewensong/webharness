#!/usr/bin/env python3
"""验证码通道的纯函数与节流逻辑自测（不联网、不需要 pytest）。

用法: python3 -m scripts.unit_sms

覆盖：阿里云 RPC 签名、手机/邮箱规范化与掩码、限流窗口、验证码签发与核验的
节流与消费语义（用调试码短路真实短信/邮件）。
"""

import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = 0
FAIL = 0


def check(name: str, actual, expected) -> None:
    global PASS, FAIL
    if actual == expected:
        print(f"PASS  {name}")
        PASS += 1
    else:
        print(f"FAIL  {name}\n      实际: {actual!r}\n      期望: {expected!r}")
        FAIL += 1


def expect_error(name: str, status: int, fn) -> None:
    from app.verify_codes import VerifyError

    try:
        fn()
    except VerifyError as exc:
        check(name, exc.status_code, status)
    else:
        check(name, "未拒绝", status)


def section(title: str) -> None:
    print(f"\n== {title} ==")


# ---------- 阿里云 RPC 签名 ----------
section("阿里云 RPC 签名")
from app import sms  # noqa: E402

# 官方文档「签名机制」里的样例（StringToSign 与期望签名都是文档给定值）
OFFICIAL_PARAMS = {
    "Action": "DescribeRegions",
    "Format": "XML",
    "Version": "2014-05-26",
    "AccessKeyId": "testid",
    "SignatureMethod": "HMAC-SHA1",
    "Timestamp": "2016-02-23T12:46:24Z",
    "SignatureVersion": "1.0",
    "SignatureNonce": "3ee8c1b8-83d3-44af-a94f-4e0ad82fd6cf",
}
check("官方样例签名", sms._sign_params(OFFICIAL_PARAMS, "testsecret"), "OLeaidS1JvxuMvnyHOwuJ+uX5qY=")
check("百分号编码空格 -> %20", sms._percent_encode("a b"), "a%20b")
check("百分号编码 ~ 不转义", sms._percent_encode("a~b"), "a~b")
check("百分号编码 * 与 +", sms._percent_encode("a*b+c"), "a%2Ab%2Bc")
check("百分号编码冒号（时间戳）", sms._percent_encode("12:46:24Z"), "12%3A46%3A24Z")
check(
    "签名可复现",
    sms._sign_params(OFFICIAL_PARAMS, "s"),
    sms._sign_params(dict(OFFICIAL_PARAMS), "s"),
)

# ---------- 手机 / 邮箱规范化与掩码 ----------
section("手机 / 邮箱规范化与掩码")
from app import config  # noqa: E402

check("手机 11 位", config.normalize_phone("13800138000"), "13800138000")
check("手机 +86 前缀", config.normalize_phone("+86 138-0013-8000"), "13800138000")
check("手机 86 前缀", config.normalize_phone("8613800138000"), "13800138000")
check("手机 非法段号", config.normalize_phone("12345678901"), None)
check("手机 位数不足", config.normalize_phone("1380013800"), None)
check("手机 空", config.normalize_phone(""), None)
check("邮箱 小写化", config.normalize_email(" A.B+Tag@QQ.COM "), "a.b+tag@qq.com")
check("邮箱 非法", config.normalize_email("bad@"), None)
check("邮箱 空", config.normalize_email(None), None)
check("掩码 手机", config.mask_target("phone", "13800138000"), "138****8000")
check("掩码 邮箱", config.mask_target("email", "alice@qq.com"), "a***@qq.com")
check("掩码 空", config.mask_target("phone", None), None)

# ---------- 限流窗口 ----------
section("限流窗口")
from app import ratelimit  # noqa: E402

ratelimit.reset()
check("前 3 次放行", [ratelimit.hit("b", "k", 3, 0.05) for _ in range(3)], [0.0, 0.0, 0.0])
check("第 4 次被限流", ratelimit.hit("b", "k", 3, 0.05) > 0, True)
time.sleep(0.06)
check("窗口过期后放行", ratelimit.hit("b", "k", 3, 0.05), 0.0)
check("不同 key 互不影响", ratelimit.hit("b", "other", 1, 0.05), 0.0)

# ---------- 验证码签发与核验 ----------
section("验证码签发与核验（调试码短路，不联网）")
os.environ["WEBHARNESS_SMS_DEBUG_CODE"] = "246810"
os.environ["WEBHARNESS_VERIFY_MODE"] = "auto"
os.environ["WEBHARNESS_SMTP_HOST"] = "smtp.example.com"
os.environ["WEBHARNESS_SMTP_USERNAME"] = "u@example.com"
os.environ["WEBHARNESS_SMTP_PASSWORD"] = "x"

tmp = Path(tempfile.mkdtemp())
import app.db as db  # noqa: E402

db.DB_PATH = tmp / "unit.db"
db.DATA_DIR = tmp
db.UPLOADS_DIR = tmp / "uploads"
db.FILES_DIR = tmp / "files"
db.init_db()

from app import verify_codes  # noqa: E402

check("调试码使短信可用", config.sms_available(), True)
check("调试码使邮箱可用", config.email_available(), True)
check("auto 模式下通道可用即强制验证", config.verify_required(), True)

code = verify_codes.generate_code()
check("生成的码 6 位", len(code), 6)
check("生成的码不含前导零", code[0] != "0", True)
check("生成的码是数字", code.isdigit(), True)

section("签发节流与核验消费")
sent = verify_codes.send("email", "Alice@Example.com", "register")
check("签发返回有效期", sent["expiresIn"], config.CODE_TTL_SECONDS)
check("签发返回间隔", sent["interval"], config.RESEND_INTERVAL_SECONDS)
check("邮箱归一化为小写后入库", config.normalize_email("Alice@Example.com"), "alice@example.com")

expect_error("60 秒内重发被拒", 429, lambda: verify_codes.send("email", "alice@example.com", "register"))
expect_error(
    "非大陆手机号被拒", 400, lambda: verify_codes.send("phone", "12345678901", "login")
)
expect_error(
    "未知用途被拒", 400, lambda: verify_codes.send("email", "alice@example.com", "whatever")
)
expect_error(
    "错码被拒", 400, lambda: verify_codes.consume("email", "alice@example.com", "register", "000000")
)
expect_error(
    "用途不一致被拒",
    400,
    lambda: verify_codes.consume("email", "alice@example.com", "login", "246810"),
)
check(
    "调试码核验通过并返回归一化 target",
    verify_codes.consume("email", "alice@example.com", "register", "246810"),
    "alice@example.com",
)
expect_error(
    "同码不可重放",
    400,
    lambda: verify_codes.consume("email", "alice@example.com", "register", "246810"),
)

section("尝试次数上限")
verify_codes.send("phone", "13800138000", "bind")
for _ in range(config.MAX_VERIFY_ATTEMPTS):
    try:
        verify_codes.consume("phone", "13800138000", "bind", "111111")
    except verify_codes.VerifyError:
        pass
expect_error(
    "超过尝试上限后码作废",
    400,
    lambda: verify_codes.consume("phone", "13800138000", "bind", "246810"),
)

section("每日上限")
# 直接灌 10 条今日记录（sent_at 拉到 2 小时前，绕过 60s 间隔），再发应被拒
target = "13900139000"
with db.get_db() as conn:
    for i in range(config.daily_send_cap()):
        conn.execute(
            "INSERT INTO verify_codes (channel, target, purpose, code_hash, sent_at, expires_at, day_key)"
            " VALUES ('phone', ?, 'login', NULL, ?, ?, ?)",
            (
                target,
                (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
                (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
                verify_codes.day_key(),
            ),
        )
expect_error("单目标每日上限生效", 429, lambda: verify_codes.send("phone", target, "login"))

section("其他")
expect_error("未知通道被拒", 400, lambda: verify_codes._require_target("sms", "x"))
expect_error("空验证码被拒", 400, lambda: verify_codes.consume("email", "alice@example.com", "register", "  "))
check(
    "day_key 取 UTC 日期",
    verify_codes.day_key(datetime(2026, 9, 29, 23, 59, tzinfo=timezone.utc)),
    "2026-09-29",
)

print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
