"""零依赖鉴权:HS256 JWT 签发/校验 + PBKDF2 密码哈希。

不引入 PyJWT/passlib,仅用标准库(hmac/hashlib/base64/json)实现:
- 密码:PBKDF2-HMAC-SHA256,存 "pbkdf2_sha256$iters$salt_b64$hash_b64",校验恒定时间比较。
- token:HS256 JWT(header.payload.sig),payload 含 sub(用户名)/exp(过期秒)。
  每次请求若 token 有效,中间件会用 issue_token 重新签发以滑动续期。
"""
import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings

_PBKDF2_ITERATIONS = 200_000


# ── base64url(无填充) ────────────────────────────────────────────
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# ── 密码哈希 ─────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${_b64e(salt)}${_b64e(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters_s, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        iters = int(iters_s)
        salt = _b64d(salt_b64)
        expected = _b64d(hash_b64)
    except (ValueError, TypeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
    return hmac.compare_digest(dk, expected)


# ── JWT(HS256) ───────────────────────────────────────────────────
def _sign(signing_input: bytes) -> str:
    sig = hmac.new(settings.AUTH_SECRET.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return _b64e(sig)


def issue_token(username: str, ttl: Optional[int] = None) -> str:
    ttl = settings.TOKEN_TTL if ttl is None else ttl
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"sub": username, "exp": int(time.time()) + ttl}
    seg = f"{_b64e(json.dumps(header,separators=(',',':')).encode())}." \
          f"{_b64e(json.dumps(payload,separators=(',',':')).encode())}"
    return f"{seg}.{_sign(seg.encode('ascii'))}"


def verify_token(token: str) -> Optional[str]:
    """校验 token,有效返回 username,否则 None。"""
    try:
        header_b64, payload_b64, sig = token.split(".")
    except ValueError:
        return None
    expected_sig = _sign(f"{header_b64}.{payload_b64}".encode("ascii"))
    if not hmac.compare_digest(sig, expected_sig):
        return None
    try:
        payload = json.loads(_b64d(payload_b64))
    except (ValueError, TypeError):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) else None


# ── 用户存取 ─────────────────────────────────────────────────────
def authenticate(db: Session, username: str, password: str) -> bool:
    row = db.execute(
        text("SELECT password_hash FROM users WHERE username=:u"), {"u": username}
    ).first()
    if not row:
        return False
    return verify_password(password, row[0])


def seed_initial_user(db: Session) -> None:
    """按 AUTH_INIT_USER/PASSWORD 播种初始用户;已存在则跳过。"""
    u = settings.AUTH_INIT_USER
    p = settings.AUTH_INIT_PASSWORD
    if not u or not p:
        return
    exists = db.execute(text("SELECT 1 FROM users WHERE username=:u"), {"u": u}).first()
    if exists:
        return
    db.execute(
        text("INSERT INTO users (username, password_hash) VALUES (:u, :h)"),
        {"u": u, "h": hash_password(p)},
    )
    db.commit()
