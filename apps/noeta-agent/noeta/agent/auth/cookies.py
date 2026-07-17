"""dev-login session cookie: itsdangerous-signed, valid for 7 days."""
from __future__ import annotations

from typing import Optional

from itsdangerous import BadSignature, URLSafeTimedSerializer

MAX_AGE_SECONDS = 7 * 24 * 3600
_SALT = "noeta-agent-session"


def sign_session(secret: str, username: str) -> str:
    return URLSafeTimedSerializer(secret, salt=_SALT).dumps({"u": username})


def verify_session(secret: str, token: str) -> Optional[str]:
    try:
        data = URLSafeTimedSerializer(secret, salt=_SALT).loads(
            token, max_age=MAX_AGE_SECONDS
        )
    except BadSignature:
        return None
    username = data.get("u") if isinstance(data, dict) else None
    return username if isinstance(username, str) and username else None
