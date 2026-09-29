"""邮箱验证码发送（SMTP，标准库实现）。

与短信不同，邮箱没有第三方代发代核，验证码由本服务生成（见 verify_codes），
这里只负责把码送出去。QQ 邮箱要用 587 + STARTTLS（465 隐式 SSL 需显式配
WEBHARNESS_SMTP_SSL=1）。
"""

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from . import config

TIMEOUT_SECONDS = 15


class MailError(Exception):
    """邮件发送失败。"""


def _body(code: str) -> str:
    minutes = max(config.CODE_TTL_SECONDS // 60, 1)
    return (
        f"你的验证码是 {code}，{minutes} 分钟内有效。\n"
        f"请勿把验证码告诉任何人。如果不是你本人操作，忽略本邮件即可。\n"
        f"\n"
        f"Your verification code is {code}. It expires in {minutes} minutes.\n"
        f"Never share this code with anyone. If you did not request it, ignore this email.\n"
    )


def send_code(email: str, code: str) -> None:
    """把验证码发到邮箱。失败抛 MailError。"""
    if config.sms_debug_code():
        return
    cfg = config.smtp_config()
    if not cfg:
        raise MailError("邮箱服务未配置")
    message = EmailMessage()
    message["Subject"] = "WebHarness 验证码 / verification code"
    message["From"] = formataddr((str(cfg["from_name"]), str(cfg["sender"])))
    message["To"] = email
    message.set_content(_body(code))
    host, port = str(cfg["host"]), int(cfg["port"])  # type: ignore[arg-type]
    context = ssl.create_default_context()
    try:
        if cfg["ssl"]:
            with smtplib.SMTP_SSL(host, port, timeout=TIMEOUT_SECONDS, context=context) as server:
                server.login(str(cfg["username"]), str(cfg["password"]))
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=TIMEOUT_SECONDS) as server:
                server.starttls(context=context)
                server.login(str(cfg["username"]), str(cfg["password"]))
                server.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise MailError(f"邮件发送失败：{exc}") from exc
