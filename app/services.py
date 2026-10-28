import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import InboxEvent, OutboxEvent, Task, Tenant, utcnow
from .domain import (
    EXPECTED_TENANT_FOR_TASK,
    TENANT_FOR_TASK,
    TERMINAL_TASKS,
    TaskStatus,
    TaskType,
    TenantStatus,
)
from .schemas import ResultEvent

LOG = logging.getLogger(__name__)


class DomainError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        self.code, self.message, self.details = code, message, details or {}
        super().__init__(message)


def task_payload(task: Task) -> dict:
    return {
        "id": task.id,
        "type": task.type,
        "tenant_id": task.tenant_id,
        "status": task.status,
        "created_at": task.created_at.isoformat(),
    }


def make_task(session: Session, tenant: Tenant, kind: TaskType) -> Task:
    task = Task(type=kind.value, tenant_id=tenant.id, status=TaskStatus.ACCEPTED.value)
    session.add(task)
    session.flush()
    session.add(OutboxEvent(task_id=task.id, payload=task_payload(task)))
    return task


def create_tenant(session: Session, slug: str, name: str) -> tuple[Tenant, Task]:
    LOG.info("Creating tenant", extra={"slug": slug, "tenant_name": name})
    tenant = Tenant(slug=slug, name=name, status=TenantStatus.PROVISIONING.value)
    try:
        session.add(tenant)
        session.flush()
        task = make_task(session, tenant, TaskType.DEPLOY)
        session.commit()
        LOG.info(
            "Tenant created successfully",
            extra={
                "tenant_id": tenant.id,
                "slug": slug,
                "task_id": task.id,
                "task_type": task.type,
            },
        )
    except IntegrityError as exc:
        session.rollback()
        LOG.warning("Tenant creation failed - slug already exists", extra={"slug": slug})
        raise DomainError(
            "tenant_already_exists", "A tenant with this slug already exists."
        ) from exc
    return tenant, task


def get_tenant(session: Session, tenant_id: str) -> Tenant:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise DomainError("tenant_not_found", "Tenant was not found.")
    return tenant


def request_update(
    session: Session, tenant_id: str, name: str, version: int
) -> tuple[Tenant, Task]:
    LOG.info(
        "Requesting tenant update",
        extra={"tenant_id": tenant_id, "tenant_name": name, "version": version},
    )
    tenant = get_tenant(session, tenant_id)
    if tenant.version != version:
        LOG.warning(
            "Tenant update failed - version conflict",
            extra={
                "tenant_id": tenant_id,
                "expected_version": version,
                "current_version": tenant.version,
            },
        )
        raise DomainError(
            "tenant_version_conflict",
            "Tenant has changed since it was retrieved.",
            {"expected_version": version, "current_version": tenant.version},
        )
    if tenant.status != TenantStatus.ACTIVE.value:
        LOG.warning(
            "Tenant update failed - not active",
            extra={"tenant_id": tenant_id, "status": tenant.status},
        )
        raise DomainError("tenant_update_not_allowed", "Tenant is not active.")
    # Version check happens again in the conditional UPDATE, preventing lost updates.
    from sqlalchemy import update

    write_result = session.execute(
        update(Tenant)
        .where(
            Tenant.id == tenant_id,
            Tenant.version == version,
            Tenant.status == TenantStatus.ACTIVE.value,
        )
        .values(
            name=name,
            status=TenantStatus.UPDATING.value,
            version=Tenant.version + 1,
            updated_at=utcnow(),
        )
    )
    changed = write_result.rowcount  # type: ignore[attr-defined]
    if changed != 1:
        session.rollback()
        LOG.warning(
            "Tenant update failed - conditional update conflict",
            extra={"tenant_id": tenant_id, "version": version},
        )
        raise DomainError("tenant_version_conflict", "Tenant has changed since it was retrieved.")
    tenant = get_tenant(session, tenant_id)
    task = make_task(session, tenant, TaskType.UPDATE)
    session.commit()
    LOG.info(
        "Tenant update requested successfully",
        extra={
            "tenant_id": tenant_id,
            "task_id": task.id,
            "new_version": tenant.version,
            "updated_tenant_name": name,
        },
    )
    return tenant, task


def request_destroy(session: Session, tenant_id: str) -> tuple[Tenant, Task]:
    LOG.info("Requesting tenant destroy", extra={"tenant_id": tenant_id})
    tenant = get_tenant(session, tenant_id)
    if tenant.status not in {TenantStatus.ACTIVE.value, TenantStatus.FAILED.value}:
        LOG.warning(
            "Tenant destroy failed - invalid state",
            extra={"tenant_id": tenant_id, "status": tenant.status},
        )
        raise DomainError(
            "tenant_update_not_allowed", "Tenant cannot be destroyed in its current state."
        )
    from sqlalchemy import update

    write_result = session.execute(
        update(Tenant)
        .where(
            Tenant.id == tenant_id,
            Tenant.status.in_([TenantStatus.ACTIVE.value, TenantStatus.FAILED.value]),
        )
        .values(
            status=TenantStatus.DESTROYING.value,
            version=Tenant.version + 1,
            updated_at=utcnow(),
        )
    )
    changed = write_result.rowcount  # type: ignore[attr-defined]
    if changed != 1:
        session.rollback()
        LOG.warning(
            "Tenant destroy failed - conditional update conflict", extra={"tenant_id": tenant_id}
        )
        raise DomainError(
            "tenant_update_not_allowed", "Tenant cannot be destroyed in its current state."
        )
    tenant = get_tenant(session, tenant_id)
    task = make_task(session, tenant, TaskType.DESTROY)
    session.commit()
    LOG.info(
        "Tenant destroy requested successfully",
        extra={"tenant_id": tenant_id, "task_id": task.id, "new_version": tenant.version},
    )
    return tenant, task


def apply_result(session: Session, event: ResultEvent) -> bool:
    """Apply one result atomically. False means duplicate/stale but safely handled."""
    LOG.debug(
        "Applying result event",
        extra={
            "event_id": event.event_id,
            "task_id": event.task_id,
            "tenant_id": event.tenant_id,
            "status": event.status,
        },
    )
    if session.get(InboxEvent, event.event_id):
        LOG.info(
            "Result event is duplicate, skipping",
            extra={"event_id": event.event_id, "task_id": event.task_id},
        )
        return False
    task = session.get(Task, event.task_id)
    if task is None or task.tenant_id != event.tenant_id:
        LOG.error(
            "Result event references unknown or mismatched task",
            extra={
                "event_id": event.event_id,
                "task_id": event.task_id,
                "tenant_id": event.tenant_id,
                "actual_tenant_id": task.tenant_id if task else None,
            },
        )
        raise DomainError("invalid_result", "Result references an unknown or mismatched task.")
    session.add(InboxEvent(event_id=event.event_id, task_id=event.task_id))
    incoming = TaskStatus(event.status)
    current = TaskStatus(task.status)
    if current in TERMINAL_TASKS or not (
        current == TaskStatus.ACCEPTED
        and incoming == TaskStatus.IN_PROGRESS
        or current == TaskStatus.IN_PROGRESS
        and incoming in TERMINAL_TASKS
    ):
        LOG.info(
            "Result event is stale or invalid transition, skipping",
            extra={
                "event_id": event.event_id,
                "task_id": event.task_id,
                "current_status": current.value,
                "incoming_status": incoming.value,
            },
        )
        session.commit()
        return False
    task.status, task.updated_at = incoming.value, utcnow()
    if incoming in TERMINAL_TASKS:
        tenant = get_tenant(session, task.tenant_id)
        expected = EXPECTED_TENANT_FOR_TASK[TaskType(task.type)]
        if tenant.status == expected.value:
            old_status = tenant.status
            tenant.status = TENANT_FOR_TASK[(TaskType(task.type), incoming)].value
            tenant.version += 1
            tenant.updated_at = utcnow()
            LOG.info(
                "Tenant status updated by terminal task result",
                extra={
                    "tenant_id": tenant.id,
                    "task_id": task.id,
                    "task_type": task.type,
                    "task_status": incoming.value,
                    "old_tenant_status": old_status,
                    "new_tenant_status": tenant.status,
                    "new_version": tenant.version,
                },
            )
    session.commit()
    return True


def list_rows(session: Session, model, limit: int, offset: int, **filters):
    statement = select(model)
    for key, value in filters.items():
        if value is not None:
            statement = statement.where(getattr(model, key) == value)
    return session.scalars(
        statement.order_by(model.created_at.desc(), model.id.desc()).offset(offset).limit(limit)
    ).all()
