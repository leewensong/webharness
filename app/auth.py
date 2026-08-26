import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PBKDF2_ITERATIONS = 210_000
TOKEN_TTL_SECONDS = 7 * 24 * 3600
CHALLENGE_TTL_MINUTES = 5
SECRET_PATH = Path(__file__).resolve().parent.parent / "data" / "secret.key"


def _load_secret() -> bytes:
    SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SECRET_PATH.exists():
        return SECRET_PATH.read_bytes()
    secret = secrets.token_bytes(32)
    SECRET_PATH.write_bytes(secret)
    return secret


SECRET = _load_secret()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PBKDF2_ITERATIONS,
    ).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt, digest = stored.split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        int(iterations),
    ).hex()
    return hmac.compare_digest(candidate, digest)


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def create_token(user_id: int, username: str, kind: str = "human") -> str:
    payload = {
        "uid": user_id,
        "username": username,
        "kind": kind,
        "exp": int(time.time()) + TOKEN_TTL_SECONDS,
    }
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _b64encode(hmac.new(SECRET, body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}"


def parse_token(token: str) -> dict | None:
    try:
        body, signature = token.split(".", 1)
    except ValueError:
        return None
    expected = _b64encode(hmac.new(SECRET, body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(_b64decode(body))
    except (json.JSONDecodeError, ValueError):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    if "uid" not in payload or "username" not in payload:
        return None
    return {
        "id": int(payload["uid"]),
        "username": str(payload["username"]),
        "kind": payload.get("kind", "human"),
    }


def normalize_agent_public_key(text: str) -> str:
    """解析 PEM 或 OpenSSH Ed25519 公钥，规范化为 PEM 存储。"""
    raw = text.strip().replace("\r\n", "\n")
    try:
        if raw.startswith("ssh-"):
            key = serialization.load_ssh_public_key(raw.encode("utf-8"))
        else:
            key = serialization.load_pem_public_key(raw.encode("utf-8"))
    except ValueError as exc:
        raise ValueError(
            "公钥格式无效。请粘贴 PEM（-----BEGIN PUBLIC KEY-----）或 ssh-ed25519 开头的公钥"
        ) from exc
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("公钥必须是 Ed25519（openssl genpkey -algorithm ed25519）")
    return key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


def new_nonce() -> str:
    return secrets.token_urlsafe(32)


def verify_agent_signature(public_pem: str, message: str, signature_b64: str) -> bool:
    try:
        key = serialization.load_pem_public_key(public_pem.encode("utf-8"))
        if not isinstance(key, Ed25519PublicKey):
            return False
        signature = base64.b64decode(signature_b64)
        key.verify(signature, message.encode("utf-8"))
        return True
    except (InvalidSignature, ValueError):
        return False
