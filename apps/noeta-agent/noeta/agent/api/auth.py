"""Auth endpoints: dev-login / me / logout / config. dev-login is gated by
the dev_login_enabled dynamic config (403 when disabled)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from noeta.agent.auth.cookies import MAX_AGE_SECONDS, sign_session
from noeta.agent.auth.deps import CurrentUser, get_current_user
from noeta.agent.config_registry import config_value

router = APIRouter(prefix="/auth", tags=["auth"])


class DevLoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)


@router.get("/config")
async def auth_config(request: Request) -> dict:
    """Public endpoint: the frontend login page configures its login UI from
    this (the dev-login toggle plus provider-contributed fields)."""
    provider = request.app.state.auth_provider
    return {
        # dev_login_enabled goes through dynamic config (hot-switchable from
        # the admin console); when not overridden it falls back to the static
        # Settings value.
        "dev_login_enabled": config_value(request, "dev_login_enabled"),
        **provider.login_options(),
    }


@router.post("/dev-login")
async def dev_login(body: DevLoginBody, request: Request, response: Response) -> dict:
    settings = request.app.state.settings
    if not config_value(request, "dev_login_enabled"):
        raise HTTPException(status_code=403, detail="dev-login is disabled")
    username = body.username.strip()
    if not username:
        raise HTTPException(status_code=422, detail="Username must not be empty")
    response.set_cookie(
        key=settings.session_cookie_name,
        value=sign_session(settings.session_secret, username),
        max_age=MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
    )
    user_store = getattr(request.app.state, "user_store", None)
    if user_store is not None:
        user_store.upsert_user(username)
    return {"user": {"username": username, "email_prefix": username}}


@router.get("/me")
async def me(user: CurrentUser = Depends(get_current_user)) -> dict:
    return {
        "user": {
            "username": user.username,
            "email": user.email,
            "email_prefix": user.email_prefix,
            "name": user.name,
            "avatar": user.avatar,
            "is_admin": user.is_admin,
        }
    }


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    settings = request.app.state.settings
    response.delete_cookie(settings.session_cookie_name)
    return {"ok": True}
