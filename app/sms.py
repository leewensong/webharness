"""阿里云「号码认证服务 PNVS」短信验证码客户端。

验证码由阿里云生成并核验（本服务不存码、不落日志），核验接口免费。
选 PNVS 而非普通短信服务的原因：个人实名账号即可开通、签名与模板系统赠送、
无需企业资质（详见 clip/ALIYUN-SMS.md 的选型说明）。

不引官方 SDK：阿里云 RPC 签名就是一轮 HMAC-SHA1，标准库足够，且符合本项目
「零第三方依赖」的取向。
"""

import base64
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from . import config

ENDPOINT = "https://dypnsapi.aliyuncs.com/"
API_VERSION = "2017-05-25"
TIMEOUT_SECONDS = 15


class SmsError(Exception):
    """发送失败，或核验时无法与阿里云通信（无法判定 = 不算用户输错）。"""


def _percent_encode(value: object) -> str:
    """阿里云 RPC 规定的百分号编码：RFC3986，`~` 不编码，空格编成 %20。"""
    return quote(str(value), safe="~")


def _sign_params(params: dict[str, str], ak_secret: str) -> str:
    """阿里云 RPC 签名（HMAC-SHA1）。纯函数，便于单测。"""
    canonical = "&".join(
        f"{_percent_encode(key)}={_percent_encode(params[key])}" for key in sorted(params)
    )
    string_to_sign = "GET&%2F&" + _percent_encode(canonical)
    digest = hmac.new(
        (ak_secret + "&").encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def _call(action: str, biz_params: dict[str, str], cfg: dict[str, str]) -> dict:
    params = {
        "Action": action,
        "Version": API_VERSION,
        "Format": "JSON",
        "AccessKeyId": cfg["ak_id"],
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": uuid.uuid4().hex,
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    params.update(biz_params)
    params["Signature"] = _sign_params(params, cfg["ak_secret"])
    url = ENDPOINT + "?" + "&".join(
        f"{_percent_encode(key)}={_percent_encode(value)}" for key, value in params.items()
    )
    try:
        with urlopen(Request(url, method="GET"), timeout=TIMEOUT_SECONDS) as resp:
            payload = resp.read().decode("utf-8")
    except HTTPError as exc:
        # 业务错误（如 isv.INVALID_PARAMETERS）也走 HTTP 4xx，正文里才有 Code/Message
        try:
            payload = exc.read().decode("utf-8")
        except OSError as read_exc:
            raise SmsError(f"短信服务返回 HTTP {exc.code}") from read_exc
    except (URLError, TimeoutError, OSError) as exc:
        raise SmsError(f"短信服务连接失败：{exc}") from exc
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SmsError("短信服务返回了无法解析的内容") from exc
    if not isinstance(data, dict):
        raise SmsError("短信服务返回了非预期的内容")
    return data


def send_code(phone: str) -> None:
    """发送注册/登录验证码。失败抛 SmsError（调用方不应留下额度记录）。"""
    if config.sms_debug_code():
        return
    cfg = config.sms_config()
    if not cfg:
        raise SmsError("短信服务未配置")
    minutes = max(config.CODE_TTL_SECONDS // 60, 1)
    data = _call(
        "SendSmsVerifyCode",
        {
            "PhoneNumber": phone,
            "SignName": cfg["sign_name"],
            "TemplateCode": cfg["template_code"],
            # ⚠️ 必须是 ##code## 占位符：阿里云据此动态生成码并留底核验。
            # 传自定义码（或 ${code} / 空值）会报模版变量非法，且阿里云无法核验。
            "TemplateParam": json.dumps(
                {"code": "##code##", "min": str(minutes)}, ensure_ascii=False
            ),
            "CodeType": "1",
            "CodeLength": str(config.CODE_LENGTH),
            # ⚠️ ValidTime 的单位是秒（不是分钟），要与模板里的 ${min} 换算一致
            "ValidTime": str(config.CODE_TTL_SECONDS),
            "Interval": str(config.RESEND_INTERVAL_SECONDS),
            "CountryCode": "86",
        },
        cfg,
    )
    if str(data.get("Code", "")).upper() != "OK":
        message = data.get("Message") or data.get("Code") or "未知错误"
        raise SmsError(f"短信发送失败：{message}")


def check_code(phone: str, code: str) -> bool:
    """核验验证码。False = 码不对；抛 SmsError = 无法判定（不计尝试次数）。"""
    debug = config.sms_debug_code()
    if debug:
        return hmac.compare_digest(code.strip().encode(), debug.encode())
    cfg = config.sms_config()
    if not cfg:
        raise SmsError("短信服务未配置")
    data = _call(
        "CheckSmsVerifyCode",
        {
            "PhoneNumber": phone,
            "VerifyCode": code.strip(),
            # 1 = 不区分大小写（数字码下等价于严格比较；0 是无效值，别用）
            "CaseAuthPolicy": "1",
            "CountryCode": "86",
        },
        cfg,
    )
    model = data.get("Model")
    if not isinstance(model, dict):
        model = {}
    return str(model.get("VerifyResult", "")).upper() == "PASS"
