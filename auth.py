"""Demo 登入驗證與簽章 Cookie 工具。"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time


PASSWORD_ITERATIONS = 310_000
SESSION_TTL_SECONDS = 60 * 60 * 8


def hash_password(password: str) -> str:
    """使用 PBKDF2 與隨機鹽值保存密碼，不把明文密碼寫進資料庫。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
    return f"{salt.hex()}:{digest.hex()}"


def verify_password(password: str, saved_hash: str) -> bool:
    """用常數時間比較驗證輸入密碼，降低計時資訊外洩。"""
    try:
        salt_hex, digest_hex = saved_hash.split(":", 1)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), PASSWORD_ITERATIONS)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _secret_key() -> bytes:
    """讀取本機 Demo 的 Cookie 簽章金鑰。"""
    return os.getenv("SECRET_KEY", "local-demo-key-change-me").encode()


def create_session_token(user_id: int, role: str, display_name: str) -> str:
    """建立有有效期限且不可竄改的登入 Cookie 內容。"""
    payload = json.dumps(
        {"id": user_id, "role": role, "name": display_name, "expires": int(time.time()) + SESSION_TTL_SECONDS},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(_secret_key(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def read_session_token(token: str | None) -> dict | None:
    """驗證 Cookie 簽章與期限，成功時回傳登入者識別資料。"""
    if not token or "." not in token:
        return None
    encoded, supplied_signature = token.rsplit(".", 1)
    expected_signature = hmac.new(_secret_key(), encoded.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        return None
    try:
        padding = "=" * (-len(encoded) % 4)
        session = json.loads(base64.urlsafe_b64decode(encoded + padding).decode())
        if session["role"] not in {"author", "editor"} or int(session["expires"]) < int(time.time()):
            return None
        return {"id": int(session["id"]), "role": session["role"], "display_name": session["name"]}
    except (KeyError, TypeError, ValueError, UnicodeDecodeError):
        return None
