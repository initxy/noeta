"""FastAPI auth dependencies: the provider seam is tried first, then the
dev-login session cookie."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, Request

from noeta.agent.auth.cookies import verify_session
from noeta.agent.auth.provider import AuthProvider, AuthUser
from noeta.agent.config import Settings


@dataclass
class CurrentUser:
    username: str
    email: Optional[str] = None
    name: Optional[str] = None
    avatar: Optional[str] = None
    email_prefix: str = ""
    # Admin allowlist hit (ADMIN_USERS): grants access to the admin console.
    # See config.admin_user_set.
    is_admin: bool = False


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="Not logged in")


def _email_prefix(email: Optional[str], username: str) -> str:
    if not isinstance(email, str) or not email:
        return username
    if "@" in email:
        return email.split("@", 1)[0]
    return email


def _ensure_personal_space(request: Request, username: str) -> None:
    space_store = getattr(request.app.state, "space_store", None)
    if space_store is not None:
        space_store.ensure_personal_space(username)


async def get_current_user(request: Request) -> CurrentUser:
    settings: Settings = request.app.state.settings
    user_store = getattr(request.app.state, "user_store", None)
    # The identity seam goes first: a deployment-specific provider may
    # authenticate the request by non-cookie means (e.g. an SSO header).
    # None means "not mine" and falls through to the session cookie.
    provider: Optional[AuthProvider] = getattr(
        request.app.state, "auth_provider", None
    )
    if provider is not None:
        login: Optional[AuthUser] = await provider.authenticate(request)
        if login is not None:
            if user_store is not None:
                user_store.upsert_user(
                    login.username,
                    email=login.email,
                    name=login.name,
                    avatar=login.avatar,
                )
            _ensure_personal_space(request, login.username)
            return CurrentUser(
                username=login.username,
                email=login.email,
                name=login.name,
                avatar=login.avatar,
                email_prefix=_email_prefix(login.email, login.username),
                is_admin=login.username in settings.admin_user_set,
            )

    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise _unauthorized()
    username = verify_session(settings.session_secret, token)
    if not username:
        raise _unauthorized()
    if user_store is not None:
        user_store.upsert_user(username)
    _ensure_personal_space(request, username)
    return CurrentUser(
        username=username,
        email_prefix=username,
        is_admin=username in settings.admin_user_set,
    )


def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Gate for admin endpoints: non-admins always get 404 (hiding the admin
    console's existence, consistent with the 404 semantics for sessions).

    Not-logged-in raises 401 first from get_current_user; logged in but not
    on the ADMIN_USERS allowlist -> 404.
    """
    if not user.is_admin:
        raise HTTPException(status_code=404, detail="Not Found")
    return user
