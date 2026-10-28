import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from .db import Base, Task, Tenant, make_session_factory
from .logging_config import setup_logging
from .messaging import start_background_components
from .schemas import MutationView, TaskView, TenantCreate, TenantPatch, TenantView
from .services import (
    DomainError,
    create_tenant,
    get_tenant,
    list_rows,
    request_destroy,
    request_update,
)

LOG = logging.getLogger(__name__)


def create_app(database_url: str | None = None) -> FastAPI:
    engine, factory = make_session_factory(database_url)

    # Setup logging only for non-test environments
    if not database_url:
        setup_logging("api")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if database_url:
            # Test applications use isolated SQLite databases; Compose runs Alembic first.
            Base.metadata.create_all(engine)
            app.state.stop, app.state.threads = None, []
        else:
            app.state.stop, app.state.threads = start_background_components(factory)
        yield
        if app.state.stop:
            app.state.stop.set()

    app = FastAPI(title="Tenant Provisioning Control Plane", lifespan=lifespan)

    # Add request/response logging middleware
    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        tenant_id = request.path_params.get("tenant_id", "N/A")
        task_id = request.path_params.get("task_id", "N/A")
        LOG.info(
            f"API request: {request.method} {request.url.path}",
            extra={
                "method": request.method,
                "path": request.url.path,
                "tenant_id": tenant_id,
                "task_id": task_id,
            },
        )
        response = await call_next(request)
        LOG.info(
            f"API response: {response.status_code}",
            extra={
                "status_code": response.status_code,
                "method": request.method,
                "path": request.url.path,
                "tenant_id": tenant_id,
                "task_id": task_id,
            },
        )
        return response

    def db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    @app.exception_handler(DomainError)
    async def domain_error(_: Request, exc: DomainError):
        statuses = {
            "tenant_not_found": 404,
            "task_not_found": 404,
            "tenant_already_exists": 409,
            "tenant_update_not_allowed": 409,
            "tenant_version_conflict": 409,
        }
        return JSONResponse(
            status_code=statuses.get(exc.code, 422),
            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
        )

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        with factory() as session:
            session.connection()
        return {"status": "ok"}

    @app.post("/v1/tenants", response_model=MutationView, status_code=201)
    def create(body: TenantCreate, session: Session = Depends(db)):
        tenant, task = create_tenant(session, body.slug, body.name)
        return {"tenant": tenant, "task": task}

    @app.get("/v1/tenants", response_model=list[TenantView])
    def tenants(
        limit: int = 50, offset: int = 0, status: str | None = None, session: Session = Depends(db)
    ):
        return list_rows(session, Tenant, min(max(limit, 1), 100), max(offset, 0), status=status)

    @app.get("/v1/tenants/{tenant_id}", response_model=TenantView)
    def tenant(tenant_id: str, session: Session = Depends(db)):
        return get_tenant(session, tenant_id)

    @app.patch("/v1/tenants/{tenant_id}", response_model=MutationView, status_code=202)
    def patch(tenant_id: str, body: TenantPatch, session: Session = Depends(db)):
        tenant, task = request_update(session, tenant_id, body.name, body.version)
        return {"tenant": tenant, "task": task}

    @app.delete("/v1/tenants/{tenant_id}", response_model=MutationView, status_code=202)
    def delete(tenant_id: str, session: Session = Depends(db)):
        tenant, task = request_destroy(session, tenant_id)
        return {"tenant": tenant, "task": task}

    @app.get("/v1/tasks", response_model=list[TaskView])
    def tasks(
        limit: int = 50,
        offset: int = 0,
        tenant_id: str | None = None,
        status: str | None = None,
        type: str | None = None,
        session: Session = Depends(db),
    ):
        return list_rows(
            session,
            Task,
            min(max(limit, 1), 100),
            max(offset, 0),
            tenant_id=tenant_id,
            status=status,
            type=type,
        )

    @app.get("/v1/tasks/{task_id}", response_model=TaskView)
    def task(task_id: str, session: Session = Depends(db)):
        value = session.get(Task, task_id)
        if value is None:
            raise DomainError("task_not_found", "Task was not found.")
        return value

    return app


app = create_app()
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
