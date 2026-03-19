from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.core.logging import configure_logging
from app.core.settings import get_settings
from app.jobs.scheduler import SchedulerService
from app.routers import admin, api, auth, health, webhooks
from app.services.container import ServiceContainer

settings = get_settings()
configure_logging(settings)


@asynccontextmanager
async def lifespan(application: FastAPI):
    settings.media_root_path.mkdir(parents=True, exist_ok=True)
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    container = ServiceContainer.build(settings)
    scheduler_service = SchedulerService(settings, container)
    container.scheduler_service = scheduler_service
    application.state.container = container
    application.state.scheduler_service = scheduler_service
    scheduler_service.start()
    try:
        yield
    finally:
        scheduler_service.shutdown()
        await container.aclose()


app = FastAPI(title=settings.app.name, lifespan=lifespan)
if settings.app.trust_proxy_headers:
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth.router)
app.include_router(health.router)
app.include_router(webhooks.router)
app.include_router(api.router)
app.include_router(admin.router)
