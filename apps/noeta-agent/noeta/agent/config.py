"""Application settings: pydantic-settings reading the project-root .env,
with environment variables taking precedence.

Path conventions: DATA_DIR resolves relative to the application project root;
builtin skills / space skills / knowledge bases all live under SHARED_DATA_DIR
(also resolved relative to the project root). Legacy .env files may carry
retired keys; they are ignored (extra="ignore").
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# The application project root (the directory holding pyproject.toml,
# models.json, and the data/ tree). Derived from this file's location, which
# assumes an editable install; a packaged deployment overrides the paths via
# environment variables instead.
APP_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(APP_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- server ---
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"

    # --- paths ---
    data_dir: str = "data"
    # Shared data directory (knowledge materialization + skills), resolved
    # relative to the project root; backend-writable, mounted read-only into
    # sandboxes. In a multi-host deployment both sides mount the same shared
    # filesystem subtree.
    shared_data_dir: str = "data/shared"

    # --- LLM ---
    llm_provider: Literal["auto", "openai", "mock"] = "auto"
    # Model definition file (JSON, resolved relative to the project root);
    # structure documented in models_config.py.
    models_config: str = "models.json"
    # Primary gateway: any OpenAI Responses-compatible endpoint. base_url is
    # the gateway root ("/responses" is appended by the provider builder);
    # auth uses the api-key header.
    llm_base_url: str = ""
    llm_api_key: str = ""
    # Optional secondary gateway (same Responses protocol, different host +
    # Bearer auth). When both gateways are configured, build_provider returns
    # a RoutingProvider that dispatches each model to the gateway named by its
    # models.json "gateway" field. Empty = no secondary gateway; behavior is
    # unchanged. See routing_provider.py.
    secondary_llm_base_url: str = ""
    secondary_llm_api_key: str = ""
    llm_request_timeout: float = 300.0
    llm_max_tokens: int = 8192
    # Model used for async session-title generation: configured separately
    # from the chat model. Titles are a short, non-interactive task; the
    # default disables reasoning (see title.py) because reasoning models spend
    # the whole token budget thinking and starve the actual output. Not
    # generated under the mock provider. Override via TITLE_MODEL.
    title_model: str = "gpt-5.4-2026-03-05"

    # --- auth ---
    dev_login_enabled: bool = True
    session_secret: str = "noeta-agent-dev-secret-change-me"
    session_cookie_name: str = "noeta_session"
    session_cookie_secure: bool = False

    # --- admin console ---
    # Admin allowlist (comma-separated usernames): members get is_admin=True
    # and the admin console. Default empty = nobody is an admin and the admin
    # endpoints return 404 for everyone. Under dev-login anybody can log in as
    # an allowlisted username — dev-login is a development affordance; real
    # deployments plug an identity provider into the auth seam.
    admin_users: str = ""

    # --- per-session container sandbox ---
    # One local Docker AIO container per session: the side effects of the
    # standard fs/shell tools route into the container through ExecEnv.
    # Disabled by default = pure conversation mode (no containers).
    sandbox_enabled: bool = False
    # The stock open-source AIO Sandbox image. Deployments that need extra
    # tooling inside the sandbox build their own image on top and point this
    # at it.
    sandbox_image: str = "ghcr.io/agent-infra/sandbox:latest"
    sandbox_memory: str = "2g"
    sandbox_cpus: str = "2"
    # Name of the environment variable holding the container SANDBOX_API_KEY:
    # read at provisioning time, injected into the container + readiness probe
    # + ExecEnv auth, never recorded. Unset = the container runs without auth
    # (acceptable for local dev).
    sandbox_api_key_env: str = "SANDBOX_API_KEY"
    # Dedicated reverse-proxy port for the live sandbox preview panels
    # (browser/terminal/code). Must be separate from the main port: the panel
    # iframes need allow-same-origin with their own server, and serving them
    # from the main origin would hand the cookie/API surface to container
    # content. 0 = ephemeral port (the frontend reads the actual port from the
    # discovery endpoint); set explicitly when firewalls/port-forwarding need
    # a fixed port. Host follows the main HOST.
    sandbox_preview_port: int = 0
    # Two-level idle reclamation (a background reaper polls how long each
    # session has had status == 'idle'):
    # Level 1, stop — past sandbox_idle_stop_hours the container is docker
    # stop'ed: processes die and the memory/cpu go back to the host (the whole
    # point of reclamation), but the container, its write layer, mounts and
    # port mappings all remain. Resuming a conversation re-attaches with
    # docker start (seconds, in-container state preserved) — resume goes
    # through attach, not fresh allocation, so a stopped container must stay
    # attachable and must not be rm'ed (see docker_sandbox.attach).
    # Level 2, remove — past sandbox_idle_remove_hours the container is
    # docker rm'ed, reclaiming the disk too (the one thing stop leaves). After
    # that the session cannot re-attach (the container is gone) and only a
    # fresh session works, so this level should be much longer than stop.
    # 0 / negative disables that level; both disabled = no reaper (containers
    # live until session deletion / process exit).
    sandbox_idle_stop_hours: float = 1.0
    sandbox_idle_remove_hours: float = 24.0
    # Reaper poll interval (hours): defaults to 1/10 of the stop level, with a
    # one-minute floor.
    sandbox_idle_check_interval_hours: float = 0.1

    # --- global switches for the agent tool surface ---
    # (Temporary until real per-space switches land.) When off, the matching
    # tools stay out of the compiled main spec for every space — tools are
    # registered statically at Client init and there is no per-space runtime
    # seam. These flags retire once per-space precompiled main variants exist.
    # All off by default.
    memory_tools_enabled: bool = False   # memory_write/read/search/archive + consolidation
    collab_tools_enabled: bool = False   # channel_read_history/topic + board_*
    subagent_enabled: bool = False       # spawn_subagent (explorer/web delegation)

    # --- memory consolidation (background memory maintenance) ---
    # Triggered at turn boundaries, debounced per space (the marker lives in
    # each space's memory directory); the consolidation agent gets only the
    # memory tool surface. Off = memories are written but never consolidated
    # (the interactive surface is unaffected). Only effective when
    # memory_tools_enabled is on (nothing to consolidate otherwise).
    memory_consolidation: bool = True
    memory_consolidation_debounce_hours: float = 24.0

    # --- noeta worker pool (single host, multiple workers) ---
    # Number of resident WorkerLoop threads in the noeta Client:
    # start/send_goal/answer hand the lease back to the pool through
    # seed_* + _yield_seeded_lease, and N workers drive different sessions'
    # turns concurrently (turns within one session stay serialized by the
    # dispatcher lease). The mock/test path is unaffected (FakeLLM is
    # concurrency-safe). Set 1 to degrade to a single worker.
    agent_num_workers: int = 4

    # ------------------------------------------------------------------
    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        return p if p.is_absolute() else APP_DIR / p

    @property
    def workspaces_path(self) -> Path:
        return self.data_path / "workspaces"

    @property
    def memories_path(self) -> Path:
        """Space memory root: memories/<space_id>/ is one memory pool per
        space (host side; never mounted into containers)."""
        return self.data_path / "memories"

    @property
    def shared_data_path(self) -> Path:
        p = Path(self.shared_data_dir)
        return p if p.is_absolute() else APP_DIR / p

    @property
    def knowledge_path(self) -> Path:
        return self.shared_data_path / "knowledge"

    @property
    def space_skills_path(self) -> Path:
        return self.shared_data_path / "space-skills"

    @property
    def builtin_skills_path(self) -> Path:
        """Shared builtin-skills directory (backend-writable, mounted
        read-only into sandboxes). Builtin skills do not ship with the code;
        they are managed entirely through the admin console. Empty out of the
        box, no seed."""
        return self.shared_data_path / "builtin-skills"

    @property
    def app_db_path(self) -> Path:
        return self.data_path / "app.db"

    @property
    def noeta_db_path(self) -> Path:
        return self.data_path / "noeta.db"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def admin_user_set(self) -> set[str]:
        return {u.strip() for u in self.admin_users.split(",") if u.strip()}

    @property
    def models_config_path(self) -> Path:
        p = Path(self.models_config)
        return p if p.is_absolute() else APP_DIR / p

    @property
    def effective_provider(self) -> str:
        """What "auto" resolves to: the gateway when credentials are present,
        otherwise the offline mock."""
        if self.llm_provider == "auto":
            if self.llm_base_url and self.llm_api_key:
                return "openai"
            return "mock"
        return self.llm_provider

    @property
    def secondary_gateway_configured(self) -> bool:
        """Whether the secondary gateway is usable: base_url + api_key both
        set. Routing only stacks on top of an active primary gateway (the
        secondary never stands alone)."""
        return bool(self.secondary_llm_base_url and self.secondary_llm_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
