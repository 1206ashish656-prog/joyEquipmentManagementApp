"""
Password hashing and signed session cookies for the dashboard's OWN login
— entirely separate from the target application's credentials (requirement
#17: this app's secrets are its own concern, .env-only credentials are a
different thing entirely).

Deliberately stdlib-only (hashlib/hmac), no extra dependency (passlib/
bcrypt/itsdangerous, etc.) — one less thing to install/fail to install on
an arbitrary machine, and PBKDF2-HMAC-SHA256 with a high iteration count
is still a reasonable, well-understood choice for this scale.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

_PBKDF2_ITERATIONS = 390_000
_ALGO = "pbkdf2_sha256"


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ITERATIONS)
    return f"{_ALGO}${_PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored_hash: str | None) -> bool:
    if not stored_hash:
        return False
    try:
        algo, iterations_s, salt, hex_digest = stored_hash.split("$")
        if algo != _ALGO:
            return False
        iterations = int(iterations_s)
    except ValueError:
        return False

    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
    return hmac.compare_digest(candidate.hex(), hex_digest)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def sign_session(user_id: int, secret_key: str) -> str:
    """Cookie value: base64url(user_id:timestamp).hex_hmac"""
    payload = f"{user_id}:{int(time.time())}"
    payload_b64 = _b64url_encode(payload.encode("utf-8"))
    signature = hmac.new(secret_key.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{signature}"


def verify_session(token: str | None, secret_key: str, max_age_seconds: int = 7 * 24 * 3600) -> int | None:
    """Returns the user_id if the token is valid and not expired, else None.
    Never raises — a malformed/tampered cookie is just treated as "not
    logged in", matching the rest of this codebase's rule that uncertainty
    never becomes a false-positive state."""
    if not token or "." not in token:
        return None
    payload_b64, _, signature = token.partition(".")
    expected_signature = hmac.new(secret_key.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        return None

    try:
        payload = _b64url_decode(payload_b64).decode("utf-8")
        user_id_s, issued_at_s = payload.split(":")
        user_id = int(user_id_s)
        issued_at = int(issued_at_s)
    except (ValueError, UnicodeDecodeError):
        return None

    if time.time() - issued_at > max_age_seconds:
        return None

    return user_id
