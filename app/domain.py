from enum import StrEnum


class TenantStatus(StrEnum):
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    UPDATING = "updating"
    DESTROYING = "destroying"
    FAILED = "failed"
    DESTROYED = "destroyed"


class TaskStatus(StrEnum):
    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"


class TaskType(StrEnum):
    DEPLOY = "deploy"
    UPDATE = "update"
    DESTROY = "destroy"


TERMINAL_TASKS = {TaskStatus.DONE, TaskStatus.FAILED}
TENANT_FOR_TASK = {
    (TaskType.DEPLOY, TaskStatus.DONE): TenantStatus.ACTIVE,
    (TaskType.DEPLOY, TaskStatus.FAILED): TenantStatus.FAILED,
    (TaskType.UPDATE, TaskStatus.DONE): TenantStatus.ACTIVE,
    (TaskType.UPDATE, TaskStatus.FAILED): TenantStatus.FAILED,
    (TaskType.DESTROY, TaskStatus.DONE): TenantStatus.DESTROYED,
    (TaskType.DESTROY, TaskStatus.FAILED): TenantStatus.FAILED,
}
EXPECTED_TENANT_FOR_TASK = {
    TaskType.DEPLOY: TenantStatus.PROVISIONING,
    TaskType.UPDATE: TenantStatus.UPDATING,
    TaskType.DESTROY: TenantStatus.DESTROYING,
}
