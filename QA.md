# QA Execution Guide

## Scope

This guide validates tenant lifecycle behavior, concurrency protection, durable messaging, and local operational workflows. Record the execution date, executor, environment, and actual result alongside each case.

## Test environment

| Item | Value |
| --- | --- |
| API | `http://localhost:8000` |
| RabbitMQ UI | `http://localhost:15672` (`guest` / `guest`) |
| Boot command | `make up` |
| Quality/test command | `make test` |
| Reset command | `make reset` — destructive: removes the local Compose database volume |

For a clean database, run `make reset` followed by `make up`. Wait until `curl -fsS http://localhost:8000/health/ready` returns `{"status":"ok"}`.

## Execution record

| Field | Value |
| --- | --- |
| Date / time | |
| Executor | |
| Git revision | |
| Environment | Local Docker Compose |
| Result | Pass / Fail / Blocked |

## API and lifecycle tests

| ID | Test | Steps | Expected result | Evidence / actual result |
| --- | --- | --- | --- | --- |
| API-01 | Create tenant | `POST /v1/tenants` with `{"slug":"qa-acme","name":"QA Acme"}`. | `201`; tenant is `provisioning`, version `1`; returned task is `deploy` and `accepted`. | |
| API-02 | Validation | Submit a blank name and an invalid slug such as `bad`. | `422` with a structured validation response; no tenant/task is created. | |
| API-03 | Slug uniqueness | Repeat API-01 with the same slug, including after its tenant reaches `destroyed`. | `409`; `error.code` is `tenant_already_exists`. | |
| API-04 | Read/list/filter | Call `GET /v1/tenants`, `GET /v1/tenants/{id}`, `GET /v1/tasks?tenant_id={id}`, and `GET /v1/tasks/{taskId}`. | Returned resources match the create response; task filtering returns only matching tasks. | |
| API-05 | Not found errors | Request a random tenant and task ID. | `404` with `tenant_not_found` and `task_not_found`, respectively. | |
| LIFE-01 | Deploy succeeds | Create a tenant and poll `GET /v1/tenants/{id}` and `GET /v1/tasks/{taskId}`. | Task progresses `accepted → in_progress → done`; tenant progresses `provisioning → active`; tenant version is `2`. | |
| LIFE-02 | Update succeeds | From an active tenant, `PATCH /v1/tenants/{id}` with `{"name":"QA Acme Updated","version":2}`; poll the update task. | `202`; tenant becomes `updating`, then `active`; update task is `done`; name changes; version becomes `4`. | |
| LIFE-03 | Destroy succeeds | `DELETE /v1/tenants/{id}` after LIFE-02; poll destroy task. | `202`; tenant becomes `destroying`, then `destroyed`; task is `done`; version becomes `6`. | |
| LIFE-04 | State guard | PATCH a provisioning, updating, destroying, failed, or destroyed tenant. | `409` with `tenant_update_not_allowed`; no extra task exists. | |
| LIFE-05 | Delete state guard | DELETE a provisioning, updating, destroying, or destroyed tenant. | `409` with `tenant_update_not_allowed`; no extra task exists. | |

## Failure, idempotency, and ordering

| ID | Test | Steps | Expected result | Evidence / actual result |
| --- | --- | --- | --- | --- |
| MSG-01 | Forced deploy failure | Stop worker, launch `docker compose run --no-deps -d --name qa-failing-worker worker python -m app.worker --fail-rate=1 --min-delay-ms=0 --max-delay-ms=0`, create a tenant, then restore normal worker with `docker rm -f qa-failing-worker && docker compose up -d worker`. | Task is `failed`; tenant is `failed`; version increments from `1` to `2`. | |
| MSG-02 | Update failure | With a tenant first made active, run the forced-failure worker and PATCH it. | Update task is `failed`; tenant becomes `failed`; name/version reflect the accepted API mutation plus terminal result. | |
| MSG-03 | Destroy failure | With an active or failed tenant, run the forced-failure worker and DELETE it. | Destroy task is `failed`; tenant becomes `failed`, not `destroyed`. | |
| MSG-04 | Duplicate result | Publish the same valid result envelope twice to `provisioning.results` using RabbitMQ UI. | First message applies once; duplicate is acknowledged with no second task/tenant mutation or version increase. | |
| MSG-05 | Stale result | After a task is terminal, publish a new `in_progress` result for that task with a different `event_id`. | Task and tenant states do not regress; message is handled safely. | |
| MSG-06 | Direct terminal result | Publish `done` for an `accepted` task without first publishing `in_progress`. | Task remains `accepted`; tenant remains in its prior state; event is recorded as safely handled. | |
| MSG-07 | Mismatched tenant | Publish a result with a real task ID and different tenant ID. | Message is moved to `provisioning.dead-letter`; consumer remains alive. | |
| MSG-08 | Malformed result | Publish invalid JSON to `provisioning.results`. | Message is moved to `provisioning.dead-letter`; consumer continues handling subsequent valid messages. | |

Example result envelope for MSG-04 through MSG-07:

```json
{
  "event_id": "replace-with-a-unique-or-duplicate-id",
  "task_id": "task-id",
  "tenant_id": "tenant-id",
  "status": "in_progress"
}
```

## Concurrency and durability

| ID | Test | Steps | Expected result | Evidence / actual result |
| --- | --- | --- | --- | --- |
| CON-01 | Concurrent create | Send two simultaneous `POST /v1/tenants` calls using the same slug. | Exactly one `201`; exactly one `409 tenant_already_exists`; one tenant and deploy task persist. | |
| CON-02 | Concurrent PATCH | Make a tenant active, then send two simultaneous PATCH requests with the same version. | Exactly one `202`; the other response is `409 tenant_version_conflict` or state guard conflict; one update task exists. | |
| CON-03 | Concurrent DELETE | Make a tenant active, then send two simultaneous DELETE requests. | Exactly one `202`; the other response is `409`; one destroy task exists. | |
| DB-01 | Restart persistence | Create a tenant, run `docker compose restart api`, then get tenant/task. | Tenant, task, statuses, timestamps, and versions persist. | |
| DB-02 | Outbox recovery | Stop API immediately after a create response, restart it, then start/observe worker processing. | Committed task is eventually published and reaches a terminal state; no task is lost. | |
| DB-03 | Broker recovery | Stop RabbitMQ, create a tenant, confirm API write succeeds, restart RabbitMQ. | Outbox event remains pending while unavailable and eventually publishes; tenant completes after recovery. | |

## Quality gates

| ID | Test | Steps | Expected result | Evidence / actual result |
| --- | --- | --- | --- | --- |
| QG-01 | Automated suite | Run `make test`. | Ruff, formatting, mypy, Bandit (documented exclusions), and pytest all pass. | |
| QG-02 | Compose validation | Run `docker compose config -q`. | Exit code `0`. | |
| QG-03 | Cold boot / migration | On a disposable local environment, run `make reset && make up`, then inspect API readiness and `alembic_version` in PostgreSQL. | API becomes ready; migration revision is `0001_initial`; lifecycle tests execute successfully. | |

## Exit criteria

The release is acceptable when all API, lifecycle, messaging, concurrency, persistence, and quality-gate cases pass. A failure in any of CON-01 through CON-03, LIFE-01 through LIFE-03, MSG-04 through MSG-08, DB-01 through DB-03, or QG-03 is a release blocker because it affects state correctness, data durability, or at-least-once messaging safety.
