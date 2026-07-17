"""Application entry: the create_app factory + a module-level app
(``uvicorn noeta.agent.main:app``).

- ``/api/v1/*``: REST + SSE.
- Built-frontend hosting: when a frontend dist directory exists, the SPA is
  mounted (unknown paths fall back to index.html for client-side routing).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from starlette.staticfiles import StaticFiles

from noeta.agent.api import admin as admin_api
from noeta.agent.api import auth as auth_api
from noeta.agent.api import board as board_api
from noeta.agent.api import channels as channels_api
from noeta.agent.api import feedback as feedback_api
from noeta.agent.api import knowledge as knowledge_api
from noeta.agent.api import memories as memories_api
from noeta.agent.api import misc as misc_api
from noeta.agent.api import sessions as sessions_api
from noeta.agent.api import skills as skills_api
from noeta.agent.api import space_skills as space_skills_api
from noeta.agent.api import spaces as spaces_api
from noeta.agent.api import templates as templates_api
from noeta.agent.auth.provider import build_auth_provider
from noeta.agent.config import APP_DIR, Settings, get_settings
from noeta.agent.host.service import AgentService
from noeta.agent.services.channels import ChannelService
from noeta.agent.services.knowledge_sync import KnowledgeSyncManager
from noeta.agent.store.agent_config import AgentConfigStore
from noeta.agent.store.app_config import AppConfigStore
from noeta.agent.store.board import BoardStore
from noeta.agent.store.channels import ChannelStore
from noeta.agent.store.feedback import FeedbackStore
from noeta.agent.store.knowledge import KnowledgeSourceStore
from noeta.agent.store.sessions import SessionStore
from noeta.agent.store.skills import SkillStore
from noeta.agent.store.spaces import SpaceStore
from noeta.agent.store.templates import TemplateStore
from noeta.agent.store.users import UserStore

logger = logging.getLogger(__name__)


class SPAStaticFiles(StaticFiles):
    """SPA static hosting: 404 falls back to index.html (client routing)."""

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


def _frontend_dist() -> Path | None:
    """Locate the built SPA. Two candidates, in order: the bundle shipped
    inside the package tree (``static/``, the wheel layout), then the
    repo-checkout dev build (``apps/web/dist``)."""
    candidates = [
        APP_DIR / "static",
        APP_DIR.parent / "web" / "dist",
    ]
    for c in candidates:
        if (c / "index.html").is_file():
            return c
    return None


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.data_path.mkdir(parents=True, exist_ok=True)
        settings.shared_data_path.mkdir(parents=True, exist_ok=True)
        store = SessionStore(settings.app_db_path)
        store.reset_stale_running()
        store.reset_stale_running_tasks()
        user_store = UserStore(settings.app_db_path)
        space_store = SpaceStore(settings.app_db_path)
        knowledge_store = KnowledgeSourceStore(settings.app_db_path)
        # Backend restart: leftover "syncing" rows reset to failed (the sync
        # threads live in-process and die with the restart).
        knowledge_store.reset_syncing_to_failed()
        # Identity seam: dev-login by default; deployments swap the provider.
        auth_provider = build_auth_provider(settings)
        skill_store = SkillStore(settings.app_db_path)
        template_store = TemplateStore(settings.app_db_path)
        # Dynamic config (admin console): a few hot-reloadable keys live in
        # app_config; reads fall back to Settings.
        app_config_store = AppConfigStore(settings.app_db_path)
        knowledge_sync_manager = KnowledgeSyncManager(knowledge_store, settings)
        # Migrate pre-space sessions into each user's personal space
        # (creating the personal space when missing).
        store.backfill_space_ids(space_store.ensure_personal_space)
        # Space agent configuration (persona etc.).
        agent_config_store = AgentConfigStore(settings.app_db_path)
        # Channels + task board (the space collaboration layer).
        channel_store = ChannelStore(settings.app_db_path)
        board_store = BoardStore(settings.app_db_path)
        # Feedback loop: finalize any analysis runs left "running" by a crash
        # (run context is in-memory only; nobody finishes them after a
        # restart).
        feedback_store = FeedbackStore(settings.app_db_path)
        feedback_store.reset_stale_running()
        service = AgentService(settings, store)
        # Feedback storage (analysis-agent suggestions + run finalization).
        service.attach_feedback_store(feedback_store)
        # Knowledge store (source-status checks when linking knowledge into a
        # session workspace).
        service.attach_knowledge_store(knowledge_store)
        # Skill registry (assembly picks link targets: global builtins union
        # the space's enabled skills).
        service.attach_skill_store(skill_store)
        # Space agent config (assembly writes the workspace AGENT.md persona).
        service.attach_agent_config_store(agent_config_store)
        # Channel service: message stream + @agent topics + status projection
        # (the read surface of the channel_read_* tools).
        channel_service = ChannelService(
            settings=settings,
            service=service,
            session_store=store,
            space_store=space_store,
            channel_store=channel_store,
            agent_config_store=agent_config_store,
        )
        service.attach_channel_service(channel_service)
        # Board tool surface (board_* tools' storage + personal-space
        # exclusion).
        service.attach_board_store(board_store)
        service.attach_space_store(space_store)
        app.state.settings = settings
        app.state.agent_config_store = agent_config_store
        app.state.session_store = store
        app.state.user_store = user_store
        app.state.space_store = space_store
        app.state.knowledge_store = knowledge_store
        app.state.knowledge_sync_manager = knowledge_sync_manager
        app.state.auth_provider = auth_provider
        app.state.skill_store = skill_store
        app.state.template_store = template_store
        app.state.app_config_store = app_config_store
        app.state.channel_store = channel_store
        app.state.channel_service = channel_service
        app.state.board_store = board_store
        app.state.feedback_store = feedback_store
        app.state.agent_service = service
        await service.startup()
        channel_service.start(asyncio.get_running_loop())
        logger.info(
            "noeta-agent backend up: http://%s:%s", settings.host, settings.port
        )
        try:
            yield
        finally:
            await channel_service.stop()
            await service.shutdown()
            knowledge_sync_manager.shutdown()
            store.close()
            user_store.close()
            space_store.close()
            knowledge_store.close()
            skill_store.close()
            template_store.close()
            app_config_store.close()
            agent_config_store.close()
            channel_store.close()
            board_store.close()
            feedback_store.close()

    app = FastAPI(title="noeta-agent", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth_api.router, prefix="/api/v1")
    app.include_router(misc_api.router, prefix="/api/v1")
    app.include_router(sessions_api.router, prefix="/api/v1")
    app.include_router(spaces_api.router, prefix="/api/v1")
    app.include_router(spaces_api.users_router, prefix="/api/v1")
    app.include_router(skills_api.router, prefix="/api/v1")
    app.include_router(templates_api.router, prefix="/api/v1")
    app.include_router(space_skills_api.router, prefix="/api/v1")
    app.include_router(knowledge_api.router, prefix="/api/v1")
    app.include_router(channels_api.router, prefix="/api/v1")
    app.include_router(board_api.router, prefix="/api/v1")
    app.include_router(memories_api.router, prefix="/api/v1")
    app.include_router(feedback_api.router, prefix="/api/v1")
    app.include_router(admin_api.router, prefix="/api/v1")

    dist = _frontend_dist()
    if dist is not None:
        app.mount("/", SPAStaticFiles(directory=str(dist), html=True), name="spa")
        logger.info("serving frontend from %s", dist)

    return app


app = create_app()
