"""Identity seam: the pluggable authentication provider.

This is the seam real deployments replace. The open-source build ships only
DevLoginProvider; a deployment plugs its own identity system in by
substituting the provider that main.py wires into app.state.auth_provider
(via build_auth_provider). The seam is intentionally small — two methods:

- login_options() contributes provider-specific fields to the public
  GET /auth/config payload (e.g. an external login URL the frontend should
  send the user to);
- authenticate(request) performs non-cookie authentication (e.g. checking a
  deployment-specific SSO header) and returns the verified profile, or None
  to fall through to the signed session cookie handled by deps.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from fastapi import Request

from noeta.agent.config import Settings


@dataclass
class AuthUser:
    """User profile returned by a successful provider authentication.

    deps.get_current_user upserts this profile into the UserStore and derives
    the request's CurrentUser (email_prefix, admin flag) from it — the same
    post-processing the session-cookie path applies to its bare username.
    """

    username: str
    email: Optional[str] = None
    name: Optional[str] = None
    avatar: Optional[str] = None


class AuthProvider(Protocol):
    def login_options(self) -> dict:
        """Provider-specific fields merged into the GET /auth/config payload
        (e.g. an external login URL). Empty dict = nothing to contribute."""
        ...

    async def authenticate(self, request: Request) -> Optional[AuthUser]:
        """Authenticate a request by non-cookie means (e.g. a
        deployment-specific SSO header). Returns the verified profile, or
        None to make the caller fall through to the signed session cookie."""
        ...


class DevLoginProvider:
    """Default provider: contributes no login options and authenticates
    nobody, so every request falls through to the dev-login session cookie."""

    def login_options(self) -> dict:
        return {}

    async def authenticate(self, request: Request) -> Optional[AuthUser]:
        return None


def build_auth_provider(settings: Settings) -> AuthProvider:
    """Construct the deployment's auth provider. The open-source build always
    returns DevLoginProvider; deployments replace this factory (or the
    app.state wiring) with their own implementation."""
    return DevLoginProvider()
