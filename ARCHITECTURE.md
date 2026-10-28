# Architecture — Tenant Provisioning Control Plane

## 1. Purpose and Scope

This document describes the architecture of the Tenant Provisioning Control Plane take-home assignment. It covers the deployable processes, persistence model, state machines, HTTP API boundaries, messaging contracts, reliability mechanisms, local runtime, testing strategy, and operational behavior.

The system provisions customer tenants asynchronously. The control plane accepts tenant lifecycle requests, persists the requested state transition and its task, publishes the task to a message broker, and applies worker progress and terminal results back to persistent state.

### 1.1 In scope

- REST API for tenant creation, listing, retrieval, partial update, and deletion requests.
- Read-only REST API for task listing and retrieval.
- PostgreSQL persistence for tenants, tasks, outbound events, and inbound-event deduplication.
- RabbitMQ messaging between the control plane and a worker simulator.
- Tenant and task state-machine enforcement.
- Optimistic concurrency control for tenant mutations.
- Durable outbound publication through a transactional outbox.
- Idempotent and order-tolerant inbound result processing.
- Retry, dead-letter, health-check, testing, and local developer workflows.

### 1.2 Out of scope

The assignment does not require a frontend, authentication and authorization, multi-region deployment, Kubernetes, a generalized workflow engine, distributed tracing, Redis, Kafka, or production-scale horizontal orchestration. These can be considered later if requirements change, but they are not part of the initial implementation.

### 1.3 Architectural principles

1. **PostgreSQL is the source of truth.** Broker messages transport work and results; they are not the authoritative state store.
2. **Persist before publishing.** A task and its outbound event are committed before the event can be published.
3. **At-least-once delivery, idempotent effects.** The system does not claim exactly-once message delivery.
4. **Enforce invariants at the database boundary.** Application checks improve error messages, while database constraints and conditional updates protect against races.
5. **Keep the domain model explicit.** Tenant and task transitions are centrally defined and tested.
6. **Separate HTTP handling from long-running work.** The API records work; the worker simulator executes it asynchronously.
7. **Prefer a small, reviewable system.** Add no infrastructure that is not needed to satisfy the assignment.

---

## 2. System Context

### 2.1 Context diagram

```mermaid
flowchart LR
    Client[API Client]
    CP[Control Plane]
    DB[(PostgreSQL)]
    Broker[(RabbitMQ)]
    Worker[Worker Simulator]

    Client -->|HTTPS / JSON| CP
    CP <-->|SQL transactions| DB
    CP -->|Provisioning task events| Broker
    Broker -->|Tasks| Worker
    Worker -->|Progress and outcome events| Broker
    Broker -->|Results| CP
```

The API client interacts only with the HTTP API. The worker simulator does not call tenant endpoints to mutate state. It consumes tasks and reports results through RabbitMQ.

### 2.2 Runtime processes

The local deployment has four services:

| Service    | Responsibility                                                                |
| ---------- | ----------------------------------------------------------------------------- |
| `api`      | FastAPI application and HTTP endpoints                                        |
| `worker`   | Separate simulator process that consumes provisioning tasks and emits results |
| `postgres` | Durable relational persistence                                                |
| `rabbitmq` | Task and result transport, acknowledgments, retries, and dead-letter routing  |

The control-plane application contains the HTTP API, domain services, repositories, transactional outbox relay, and result consumer. The relay and result consumer may run as managed background components in the API process for this assignment, but each must have a clear lifecycle and must not block the asynchronous HTTP event loop. A dedicated relay/consumer process can be introduced later without changing the domain contract.

A single Docker Compose command starts the local stack. Database migrations run before the API accepts requests. Health checks and service dependencies must prevent the application from using PostgreSQL or RabbitMQ before they are ready.

---

## 3. Logical Architecture

```mermaid
flowchart TB
    Client[HTTP Client]

    subgraph CP[Control Plane Application]
        API[FastAPI Routers]
        Schemas[Pydantic Request / Response Schemas]
        Services[Application Services]
        Domain[Domain Rules and State Machines]
        Repos[SQLAlchemy Repositories]
        OutboxRelay[Transactional Outbox Relay]
        ResultConsumer[Result Consumer]
        API --> Schemas
        API --> Services
        Services --> Domain
        Services --> Repos
        ResultConsumer --> Domain
        ResultConsumer --> Repos
        OutboxRelay --> Repos
    end

    DB[(PostgreSQL)]
    subgraph MQ[RabbitMQ]
        TaskQueue[provisioning.tasks]
        ResultQueue[provisioning.results]
        DLQ[Dead-letter queue]
    end
    Worker[Worker Simulator]

    Client --> API
    Repos <--> DB
    OutboxRelay --> TaskQueue
    TaskQueue --> Worker
    Worker --> ResultQueue
    ResultQueue --> ResultConsumer
    ResultConsumer -. poison message .-> DLQ
```

### 3.1 API layer

Responsibilities:

- Parse and validate HTTP input.
- Invoke application services.
- Map domain exceptions to stable HTTP error responses.
- Serialize resources and timestamps consistently.
- Provide OpenAPI documentation.

The API layer must not contain independent copies of lifecycle rules or publish broker events directly.

### 3.2 Application services

Responsibilities:

- Coordinate use cases and transaction boundaries.
- Apply tenant transition rules.
- Create tasks for accepted tenant mutations.
- Insert corresponding outbox records in the same transaction.
- Return stable API representations.

Suggested use cases:

- `CreateTenant`
- `ListTenants`
- `GetTenant`
- `RequestTenantUpdate`
- `RequestTenantDestroy`
- `ListTasks`
- `GetTask`
- `ApplyWorkerResult`

### 3.3 Domain layer

Responsibilities:

- Define tenant and task statuses and task types.
- Define allowed transitions.
- Validate operation eligibility.
- Define domain exceptions and invariants.
- Remain independent of FastAPI, SQLAlchemy, and RabbitMQ.

### 3.4 Persistence layer

Responsibilities:

- Map domain records to PostgreSQL.
- Execute transactions and conditional updates.
- Enforce unique slugs and event IDs.
- Implement pagination and filtering.
- Persist outbox and inbox records.

### 3.5 Messaging layer

Responsibilities:

- Publish outbox events with broker confirmation.
- Consume worker results.
- Validate message envelopes.
- Apply idempotency and transition guards.
- Retry transient failures and isolate poison messages.
- Acknowledge messages only after durable processing.

---

## 4. Technology Choices

| Concern             | Selected technology                         | Rationale                                                               |
| ------------------- | ------------------------------------------- | ----------------------------------------------------------------------- |
| Language            | Python 3.12+                                | Mature ecosystem and suitable for a focused backend service             |
| HTTP                | FastAPI                                     | Typed request validation and OpenAPI support                            |
| Validation          | Pydantic                                    | Consistent validation for HTTP and messages                             |
| Database            | PostgreSQL                                  | Transactions, constraints, row-level locking, durable storage           |
| ORM                 | SQLAlchemy 2.x                              | Explicit transaction control and conditional updates                    |
| Migrations          | Alembic                                     | Versioned and repeatable schema evolution                               |
| Broker              | RabbitMQ                                    | Work queues, manual acknowledgments, publisher confirms, dead-lettering |
| Broker client       | Pika in dedicated managed threads/processes | Keeps blocking AMQP I/O off the async HTTP event loop                   |
| Tests               | pytest, TestClient / HTTPX                  | Unit, API, integration, and concurrency tests                           |
| Lint / format       | Ruff                                        | One tool for linting and formatting                                     |
| Type checking       | mypy                                        | Static type checks                                                      |
| Security checks     | Bandit, pip-audit                           | Code and dependency checks                                              |
| Local orchestration | Docker Compose                              | One-command local startup                                               |

Dependency versions must be pinned or bounded in the project dependency lock/configuration and updated deliberately.

---

## 5. Domain Model

### 5.1 Tenant

| Field        | Type                      | Rules                                 |
| ------------ | ------------------------- | ------------------------------------- |
| `id`         | UUID                      | Server-generated primary key          |
| `slug`       | VARCHAR(28)               | Unique, including destroyed tenants   |
| `name`       | VARCHAR                   | Non-empty after trimming              |
| `status`     | Enum / constrained string | Tenant lifecycle state                |
| `version`    | INTEGER                   | Incremented for every tenant mutation |
| `created_at` | TIMESTAMPTZ               | UTC timestamp                         |
| `updated_at` | TIMESTAMPTZ               | UTC timestamp                         |

Slug validation:

```regex
^[a-z][-a-z0-9]{1,26}[a-z0-9]$
```

The accepted length is 3–28 characters. Slug uniqueness must be enforced by a PostgreSQL unique constraint. A pre-insert existence check alone is insufficient because two concurrent requests can both observe the slug as available.

Destroyed tenant rows remain persisted. They are not physically deleted by the DELETE endpoint.

The API serializes timestamps as ISO 8601 UTC strings with millisecond precision and a `Z` suffix. PostgreSQL stores timezone-aware timestamps; serialization is responsible for the exact external representation.

### 5.2 Task

| Field        | Type        | Rules                                                     |
| ------------ | ----------- | --------------------------------------------------------- |
| `id`         | UUID        | Server-generated task ID; also the outbound task event ID |
| `type`       | Enum        | `deploy`, `update`, or `destroy`                          |
| `tenant_id`  | UUID        | Foreign key to tenant                                     |
| `status`     | Enum        | `accepted`, `in_progress`, `done`, or `failed`            |
| `created_at` | TIMESTAMPTZ | UTC timestamp                                             |
| `updated_at` | TIMESTAMPTZ | UTC timestamp                                             |

Tasks are created only by tenant lifecycle API operations. HTTP task endpoints are read-only.

### 5.3 Outbox event

Suggested columns:

| Field             | Type                  | Purpose                                            |
| ----------------- | --------------------- | -------------------------------------------------- |
| `id`              | UUID                  | Outbox record ID                                   |
| `task_id`         | UUID                  | Associated task; unique for one initial task event |
| `event_type`      | VARCHAR               | Event type/version classification                  |
| `payload`         | JSONB                 | Serialized task event                              |
| `created_at`      | TIMESTAMPTZ           | Creation time                                      |
| `published_at`    | TIMESTAMPTZ, nullable | Time publication was confirmed                     |
| `attempts`        | INTEGER               | Number of publication attempts                     |
| `next_attempt_at` | TIMESTAMPTZ           | Earliest next retry time                           |
| `last_error`      | TEXT, nullable        | Sanitized operational error detail                 |

The tenant mutation, task, and outbox record are committed in one PostgreSQL transaction. The relay publishes only committed rows.

### 5.4 Inbox / processed result events

Suggested columns:

| Field         | Type        | Purpose                              |
| ------------- | ----------- | ------------------------------------ |
| `event_id`    | UUID        | Unique worker-result idempotency key |
| `task_id`     | UUID        | Task referenced by the result        |
| `received_at` | TIMESTAMPTZ | First processing time                |
| `outcome`     | VARCHAR     | Optional processing classification   |

A unique constraint on `event_id` prevents the same logical result event from being applied twice. The inbox insert and state changes must occur in the same transaction. If state processing rolls back, the inbox insert rolls back as well, allowing a legitimate retry.

### 5.5 Database constraints and indexes

At minimum:

- `UNIQUE (tenants.slug)`
- `FOREIGN KEY (tasks.tenant_id) REFERENCES tenants(id)`
- `UNIQUE (outbox_events.task_id)` for one initial task event per task
- `UNIQUE (inbox_events.event_id)`
- Index on `tasks(tenant_id, created_at)`
- Index on `tasks(status, created_at)` if task status filtering is implemented
- Index on eligible outbox records, such as `(published_at, next_attempt_at)`

PostgreSQL partial unique indexes can enforce at most one open task per tenant:

```sql
CREATE UNIQUE INDEX uq_tasks_one_open_per_tenant
ON tasks (tenant_id)
WHERE status IN ('accepted', 'in_progress');
```

This constraint is a final guard against concurrent or incorrectly coordinated operations. Tenant status guards and transactions remain the primary domain-level explanation for why a second open task is not allowed.

The partial index must be reconciled with the supported task/tenant transition rules and tested against concurrent requests.

---

## 6. State Machines and Invariants

### 6.1 Tenant state machine

```mermaid
stateDiagram-v2
    [*] --> provisioning : create / deploy task
    provisioning --> active : deploy done
    provisioning --> failed : deploy failed
    active --> updating : PATCH / update task
    updating --> active : update done
    updating --> failed : update failed
    active --> destroying : DELETE / destroy task
    failed --> destroying : DELETE / destroy task
    destroying --> destroyed : destroy done
    destroying --> failed : destroy failed
```

Allowed transitions:

| Current status | Trigger                 | Next status    |
| -------------- | ----------------------- | -------------- |
| No tenant      | Create API request      | `provisioning` |
| `provisioning` | Deploy task succeeds    | `active`       |
| `provisioning` | Deploy task fails       | `failed`       |
| `active`       | PATCH request accepted  | `updating`     |
| `updating`     | Update task succeeds    | `active`       |
| `updating`     | Update task fails       | `failed`       |
| `active`       | DELETE request accepted | `destroying`   |
| `failed`       | DELETE request accepted | `destroying`   |
| `destroying`   | Destroy task succeeds   | `destroyed`    |
| `destroying`   | Destroy task fails      | `failed`       |

No other tenant transition is allowed. In particular, a destroyed tenant cannot be reactivated or mutated.

### 6.2 Task state machine

```mermaid
stateDiagram-v2
    [*] --> accepted
    accepted --> in_progress : valid worker result
    in_progress --> done : successful terminal result
    in_progress --> failed : failed terminal result
```

`done` and `failed` are terminal. Duplicate or stale results must not move a terminal task backwards.

The PRD specifies `accepted → in_progress → done|failed`. It does not explicitly authorize a direct transition from `accepted` to a terminal state. The implementation should therefore require `in_progress` before accepting a terminal result, unless the assignment owner clarifies otherwise. An invalid result should be handled safely and should not crash the consumer.

### 6.3 Core invariants

1. A tenant slug is unique for the lifetime of its record, including after destruction.
2. A tenant has at most one open task in `accepted` or `in_progress`.
3. Every tenant mutation increments `version`, including worker-applied status changes.
4. Two PATCH requests with the same expected version cannot both succeed.
5. A task and its initial outbox event are committed atomically with the tenant transition.
6. A rolled-back API transaction does not produce a publishable outbox record.
7. Only a result for the correct task and tenant may mutate that task/tenant pair.
8. Duplicate result `event_id` values have no repeated side effects.
9. Stale results do not regress task or tenant state.
10. Terminal tasks cannot transition again.
11. Broker delivery is at least once; application effects are idempotent.
12. Errors are structured and do not expose stack traces or secrets.

---

## 7. REST API Design

Base path: `/v1`.

### 7.1 Tenant resources

| Method   | Path                      | Behavior                                           | Success status |
| -------- | ------------------------- | -------------------------------------------------- | -------------- |
| `POST`   | `/v1/tenants`             | Create tenant, deploy task, and outbox event       | `201 Created`  |
| `GET`    | `/v1/tenants`             | List tenants                                       | `200 OK`       |
| `GET`    | `/v1/tenants/{tenant_id}` | Get tenant                                         | `200 OK`       |
| `PATCH`  | `/v1/tenants/{tenant_id}` | Request update for an active tenant                | `202 Accepted` |
| `DELETE` | `/v1/tenants/{tenant_id}` | Request destruction for an active or failed tenant | `202 Accepted` |

`POST` returns the created tenant in `provisioning` state and its accepted deploy task or task reference. The exact response schema must be documented in the OpenAPI contract and README.

`PATCH` initiates an asynchronous update. It does not directly apply a worker-side change to a running environment. The API changes the tenant to `updating`, creates an `update` task, and returns the accepted task reference.

`DELETE` is a lifecycle operation rather than a physical database delete. It changes the tenant to `destroying`, creates a `destroy` task, and retains the tenant record after the task finishes.

### 7.2 Optimistic concurrency

The PATCH request carries the expected version, for example:

```json
{
  "name": "Production Environment",
  "version": 3
}
```

Within one database transaction, the service must:

1. Verify that the tenant exists.
2. Verify that its state is `active`.
3. Conditionally update the row where `id = :tenant_id AND version = :expected_version AND status = 'active'`.
4. Increment `version` and set status to `updating`.
5. Create the update task.
6. Create the outbox record.
7. Commit.

If the conditional update affects zero rows, the service distinguishes a missing tenant, a version conflict, or an invalid status without allowing a second PATCH to succeed. A row lock (`SELECT ... FOR UPDATE`) is also valid, but the final write must still preserve the version invariant.

The same transaction discipline applies to DELETE. The PRD explicitly requires version-based optimistic locking on update; it does not require a client-supplied version for DELETE. The implementation should document its DELETE concurrency policy rather than silently extend or weaken the contract.

### 7.3 Task resources

| Method | Path                  | Behavior                                        | Success status |
| ------ | --------------------- | ----------------------------------------------- | -------------- |
| `GET`  | `/v1/tasks`           | List tasks with pagination and optional filters | `200 OK`       |
| `GET`  | `/v1/tasks/{task_id}` | Get task                                        | `200 OK`       |

Task endpoints do not create, retry, cancel, or mutate tasks.

### 7.4 Pagination and filtering

Use consistent, documented query parameters, for example:

- `limit`: bounded page size with a default.
- `offset`: non-negative offset.
- `tenant_id`: filter tasks by tenant.
- `status`: filter by resource status.
- `type`: filter tasks by task type.

Sort results deterministically, such as by `created_at DESC, id DESC`, so pages are predictable when timestamps are equal. Cursor pagination may be chosen instead, but the choice must be consistent across endpoints and documented.

### 7.5 Error envelope

All errors return a stable JSON structure:

```json
{
  "error": {
    "code": "tenant_version_conflict",
    "message": "The tenant has changed since it was retrieved.",
    "details": {
      "expected_version": 3,
      "current_version": 4
    }
  }
}
```

Recommended mapping:

| Error code                        | HTTP status |
| --------------------------------- | ----------: |
| `tenant_not_found`                |         404 |
| `tenant_already_exists`           |         409 |
| `tenant_update_not_allowed`       |         409 |
| `tenant_version_conflict`         |         409 |
| `task_not_found`                  |         404 |
| Invalid request body / parameters |         422 |
| Unexpected internal error         |         500 |

Do not expose raw database errors, stack traces, credentials, or broker connection details in client responses. Log internal diagnostics with appropriate redaction and correlation identifiers.

---

## 8. Messaging Architecture

### 8.1 RabbitMQ topology

Suggested topology:

- Durable task queue: `provisioning.tasks`
- Durable result queue: `provisioning.results`
- Dead-letter queue: `provisioning.dead-letter`
- Durable exchanges and bindings appropriate to the chosen routing design

The exact exchange and routing-key names should be defined in one messaging configuration module and documented. Queues should be durable, and messages should be published as persistent where supported by the selected queue type and broker configuration.

The worker consumes task messages and publishes result messages. The control plane publishes tasks from the outbox and consumes results.

### 8.2 Outbound task event

Example:

```json
{
  "id": "8be8b87e-96fb-4b72-87b7-4821d0a4d612",
  "type": "deploy",
  "tenant_id": "6fdbcf7c-8a8b-4dcb-b1db-a813c8b1f36c",
  "status": "accepted",
  "created_at": "2026-10-26T08:30:00.123Z"
}
```

The task ID is the stable logical task/event ID. The payload must include the task ID, type, tenant ID, and status, as required by the PRD.

### 8.3 Inbound result event

Example:

```json
{
  "event_id": "fc783b6d-3ebf-4ec8-a94c-2cb2f5c7d115",
  "task_id": "8be8b87e-96fb-4b72-87b7-4821d0a4d612",
  "tenant_id": "6fdbcf7c-8a8b-4dcb-b1db-a813c8b1f36c",
  "status": "in_progress",
  "occurred_at": "2026-10-26T08:30:01.123Z"
}
```

The `event_id` is unique for each logical worker-result event and serves as the idempotency key. `task_id` identifies the task to update. The `tenant_id` is checked against the persisted task to prevent cross-tenant or mismatched updates.

### 8.4 Result processing transaction

For each result message:

1. Validate the envelope and allowed status.
2. Begin a PostgreSQL transaction.
3. Insert `event_id` into the inbox table.
4. If the unique constraint shows that the event was already processed, commit/finish duplicate handling and acknowledge the message without applying state changes.
5. Lock or conditionally update the task and confirm the message's `task_id` and `tenant_id` match persisted data.
6. Check task transition eligibility and terminal-state guards.
7. Update task status and `updated_at` if the transition is valid.
8. For a terminal outcome, update the tenant according to the task type and outcome, and increment tenant `version`.
9. Commit.
10. Acknowledge the broker message.

The inbox record and state updates must be in the same transaction. A rollback must not leave an event marked as processed when its state mutation did not commit.

Unknown task IDs, mismatched tenant IDs, malformed messages, and impossible transitions must be handled as poison or invalid messages according to the documented retry/dead-letter policy. They must not terminate the consumer process.

### 8.5 Tenant result mapping

| Task type | Task result | Tenant result            |
| --------- | ----------- | ------------------------ |
| `deploy`  | `done`      | `provisioning → active`  |
| `deploy`  | `failed`    | `provisioning → failed`  |
| `update`  | `done`      | `updating → active`      |
| `update`  | `failed`    | `updating → failed`      |
| `destroy` | `done`      | `destroying → destroyed` |
| `destroy` | `failed`    | `destroying → failed`    |

A terminal result should only mutate a tenant if the tenant is still in the expected state for that task. This prevents a stale or unrelated task result from overwriting a newer lifecycle operation.

### 8.6 Outbox relay behavior

The relay:

1. Selects eligible unpublished rows.
2. Claims rows safely if multiple relay instances may run, using row locking such as `FOR UPDATE SKIP LOCKED` or an equivalent claim mechanism.
3. Publishes the payload with persistent delivery and publisher confirmation.
4. Marks the outbox row as published after confirmation.
5. On transient failure, increments attempts and schedules exponential backoff with jitter.
6. Continues processing other rows when one row fails.

A crash after broker confirmation but before updating `published_at` can cause a duplicate publication. This is expected. The worker must tolerate duplicate task delivery, and result processing must be idempotent.

A single relay instance is sufficient for the initial local implementation. The claiming strategy should still avoid accidental concurrent publication if the code is later run with multiple replicas.

### 8.7 Worker task handling

The worker:

1. Consumes a task event.
2. Validates the task payload.
3. Publishes an `in_progress` result with a new stable `event_id`.
4. Simulates the requested operation for a configurable delay.
5. Publishes `done` or `failed` with a different `event_id`.
6. Acknowledges the task according to the selected processing policy.

The worker should acknowledge only after its result publication has been confirmed. If the worker crashes after publishing a result but before acknowledging the task, it may receive the task again. It must therefore be safe to process duplicate task deliveries. Since this is a simulator, it can maintain a small local in-memory guard during a single run, but correctness must not depend on that guard; duplicate result handling belongs in the control plane.

The simulator supports startup options such as:

```bash
python -m app.worker \
  --fail-rate 0.0 \
  --min-delay-ms 500 \
  --max-delay-ms 2000
```

A failure rate of `1.0` forces every eligible task to fail; `0.0` disables simulated failures. Delay bounds must be validated (`min >= 0`, `max >= min`). Randomized behavior is useful for demonstration, while automated tests should use deterministic outcomes where possible.

### 8.8 Retry and poison-message policy

Distinguish transient infrastructure failures from permanent invalid input:

- **Transient database or broker failure:** do not acknowledge before successful durable processing; retry with bounded exponential backoff and jitter.
- **Malformed JSON or invalid schema:** route to dead-letter handling after a bounded retry policy, or immediately if it is known to be permanently invalid.
- **Unknown task / tenant mismatch:** record safe diagnostic context and dead-letter or reject according to policy; do not retry indefinitely.
- **Duplicate event ID:** acknowledge after confirming the prior committed processing.
- **Stale result for a terminal task:** acknowledge as a harmless no-op once the message has been safely classified.
- **Unexpected exception:** log the failure, roll back the transaction, and apply bounded retry behavior. A single message must not kill the consumer.

Avoid immediate infinite `nack(requeue=True)` loops. Prefer delayed retry queues or a controlled retry mechanism with a maximum attempt count, followed by a dead-letter queue.

---

## 9. Transaction Boundaries and Failure Semantics

### 9.1 Tenant API mutation

Each state-changing tenant API call uses one PostgreSQL transaction:

```text
BEGIN
  validate tenant and expected state/version
  update tenant and increment version
  insert task(status = accepted)
  insert outbox event(payload = task)
COMMIT
```

Only after commit can the relay publish the outbox event. No API route publishes the task directly before commit.

### 9.2 Worker result application

Each worker result is handled in one transaction:

```text
BEGIN
  insert inbox event_id
  if duplicate: finish without side effects
  validate task and tenant association
  validate transition and terminal-state guard
  update task
  update tenant on terminal outcome; increment tenant.version
COMMIT
ACK message
```

The implementation may use repository methods and SQLAlchemy transaction contexts, but the atomicity boundary must remain explicit.

### 9.3 Failure matrix

| Failure point                                                   | Expected behavior                                                       |
| --------------------------------------------------------------- | ----------------------------------------------------------------------- |
| API validation fails                                            | No tenant/task/outbox writes                                            |
| Tenant/task/outbox transaction rolls back                       | No publishable event exists                                             |
| API process stops after commit                                  | Outbox relay publishes after restart                                    |
| Broker publish fails                                            | Outbox remains eligible for retry                                       |
| Broker confirms publish, relay crashes before marking published | Event may be published again; consumer/worker must tolerate duplicates  |
| Worker crashes before result publication                        | Task message is redelivered according to broker acknowledgment settings |
| Worker publishes result then crashes before task acknowledgment | Task may be redelivered; duplicate processing must be safe              |
| Result consumer fails before transaction commit                 | Transaction rolls back; message is retried                              |
| Result consumer commits then crashes before acknowledgment      | Result may be redelivered; inbox deduplication makes it a no-op         |
| Stale result arrives after terminal result                      | No state regression                                                     |
| Invalid message arrives                                         | Isolated and dead-lettered; consumer continues                          |

---

## 10. Security and Configuration

Although authentication is outside the assignment's required scope, the implementation must still follow basic secure-development practices.

- Do not commit secrets or real credentials.
- Keep `.env` out of Git and provide `.env.example` with safe local placeholders.
- Use environment variables for database and broker configuration.
- Do not expose internal stack traces in HTTP responses.
- Avoid logging passwords, connection strings, or sensitive payload data.
- Validate all external HTTP and broker input.
- Pin or constrain dependencies and run `pip-audit`.
- Run Bandit as part of the quality gate.
- Use non-default production credentials if the system is later deployed beyond local development.
- Bind local development ports deliberately; document any exposed management interface.
- Keep RabbitMQ management UI optional and local-only if enabled.

Suggested configuration values include:

```text
APP_ENV=local
DATABASE_URL=postgresql+psycopg://app:app@postgres:5432/tenant_control_plane
RABBITMQ_URL=amqp://guest:guest@rabbitmq:5672/
OUTBOX_POLL_INTERVAL_MS=500
OUTBOX_BATCH_SIZE=50
MESSAGE_MAX_ATTEMPTS=5
RETRY_BASE_DELAY_MS=250
RETRY_MAX_DELAY_MS=30000
```

These are local development examples, not production credentials or recommended production secrets.

---

## 11. Observability and Health

For this assignment, keep observability lightweight and useful.

### 11.1 Health endpoints

- `/health/live`: process is alive.
- `/health/ready`: API can use required dependencies, especially PostgreSQL. The readiness policy should also reflect broker availability if the API cannot meet its service contract without RabbitMQ, or clearly distinguish database readiness from messaging degradation.

Do not make liveness depend on RabbitMQ availability; otherwise a broker outage may cause unnecessary process restarts.

### 11.2 Structured logs

Include, where available:

- `request_id`
- `tenant_id`
- `task_id`
- `event_id`
- `outbox_event_id`
- operation or transition name
- retry attempt
- error classification

Do not log full secrets or unredacted connection strings.

### 11.3 Useful operational signals

Even if full metrics infrastructure is out of scope, expose enough logs or counters to diagnose:

- Outbox backlog and oldest unpublished event age.
- Publication retries and failures.
- Worker task processing duration.
- Result-consumer retries and dead-letter counts.
- Invalid and duplicate result events.
- Task counts by status.
- Database and broker connection failures.

These can begin as structured logs and test assertions; a metrics stack is not required for the take-home submission.

---

## 12. Testing Architecture

Testing should cover domain behavior, database semantics, and actual broker delivery. Mock-only tests are insufficient for the critical reliability guarantees.

### 12.1 Unit tests

Test:

- Slug and name validation.
- Allowed and disallowed tenant transitions.
- Allowed and disallowed task transitions.
- Error mapping.
- Message-schema validation.
- Retry-delay calculations.
- Worker argument validation.

### 12.2 API tests

Test:

- Create tenant response and initial status.
- List/get tenant.
- PATCH active tenant.
- PATCH non-active tenant.
- DELETE active or failed tenant.
- DELETE from disallowed states.
- Task list/get and not-found behavior.
- Structured errors and status codes.
- Pagination and filtering behavior.

### 12.3 Database integration tests

Run against PostgreSQL and verify:

- Slug uniqueness under concurrent creates.
- Optimistic version conflicts.
- Atomic tenant/task/outbox writes.
- Rollback leaves no task or outbox event.
- Partial unique index prevents multiple open tasks.
- Worker-applied status changes increment tenant version.
- Inbox unique constraint prevents duplicate event effects.

### 12.4 Broker integration tests

Run against RabbitMQ and verify:

- Outbox relay publishes only committed events.
- Publisher failure causes retry.
- Relay restart recovers pending events.
- Worker receives tasks and publishes progress/results.
- Duplicate task delivery is safe.
- Result consumer acknowledges only after durable processing.
- Poison messages are dead-lettered without killing the consumer.
- Stale and duplicate result events do not regress state.

### 12.5 Concurrency test

Use two independent database sessions and synchronize requests so they attempt PATCH with the same version. Exactly one request must succeed; the other must receive a version conflict or a documented state-guard conflict, but it must not also return success.

### 12.6 End-to-end test

With the complete local stack running:

1. Create a tenant through HTTP.
2. Observe the tenant in `provisioning` and task in `accepted`.
3. Observe the worker report `in_progress`.
4. Observe task `done` and tenant `active`.
5. PATCH the active tenant and verify the update task lifecycle.
6. DELETE the eligible tenant and verify `destroying → destroyed`.
7. Restart the worker with `--fail-rate 1.0` and verify failure paths on a newly created tenant.
8. Replay a duplicate or stale result and verify state does not regress.

Tests must use bounded timeouts rather than unbounded sleeps. Poll the API/database until the expected state appears or the test deadline expires.

### 12.7 Quality gates

A single documented command should run the full quality suite, including appropriate combinations of:

- Ruff lint
- Ruff format check
- mypy
- Bandit
- pip-audit
- pytest, including integration and concurrency tests

If external services are unavailable, the command should fail clearly rather than silently skip the critical integration suite.

---

## 13. Local Deployment and Developer Experience

### 13.1 One-command boot

The required cold-start workflow is:

```bash
git clone <public-repository-url>
cd <repository-directory>
make up
make test
```

The exact commands should be copied verbatim into the README.

`make up` should:

1. Build application images.
2. Start PostgreSQL and RabbitMQ.
3. Wait for dependency health checks.
4. Run database migrations.
5. Start the API, outbox relay/result consumer, and worker simulator.
6. Wait for application readiness and display useful endpoints/log instructions.

A single `docker compose up --build` command is also acceptable if the Compose configuration handles migrations and service startup correctly. Choose one canonical boot command and do not document competing approaches as the primary workflow.

### 13.2 Test command

`make test` should run the complete quality and test suite. It may use a dedicated test Compose profile or start isolated dependencies automatically. It should not rely on a previously initialized developer database.

### 13.3 Shutdown and cleanup

Provide `make down` to stop services. Document whether named volumes are retained. A separate explicit destructive cleanup command may remove local database volumes, but it should never be hidden inside the normal shutdown command.

### 13.4 Reproducible scenarios

Document commands or scripts for:

- Normal successful provisioning.
- Forced worker failure with `--fail-rate 1.0`.
- Delayed processing using minimum/maximum delay flags.
- Duplicate result delivery.
- Out-of-order or stale result delivery.
- Broker interruption and recovery.
- Outbox relay restart/recovery.

Negative scenarios must be reproducible without manual source-code changes.

---

## 14. Suggested Repository Structure

```text
tenant-provisioning-control-plane/
├── app/
│   ├── api/
│   │   ├── tenants.py
│   │   ├── tasks.py
│   │   ├── health.py
│   │   └── errors.py
│   ├── domain/
│   │   ├── enums.py
│   │   ├── transitions.py
│   │   ├── exceptions.py
│   │   └── policies.py
│   ├── schemas/
│   │   ├── tenants.py
│   │   ├── tasks.py
│   │   └── messages.py
│   ├── models/
│   │   ├── tenant.py
│   │   ├── task.py
│   │   ├── outbox.py
│   │   └── inbox.py
│   ├── repositories/
│   │   ├── tenants.py
│   │   ├── tasks.py
│   │   ├── outbox.py
│   │   └── inbox.py
│   ├── services/
│   │   ├── tenant_service.py
│   │   ├── task_service.py
│   │   └── result_service.py
│   ├── messaging/
│   │   ├── topology.py
│   │   ├── publisher.py
│   │   ├── outbox_relay.py
│   │   ├── result_consumer.py
│   │   └── retry.py
│   ├── worker/
│   │   ├── simulator.py
│   │   └── cli.py
│   ├── db.py
│   ├── config.py
│   └── main.py
├── migrations/
├── tests/
│   ├── unit/
│   ├── api/
│   ├── integration/
│   ├── concurrency/
│   └── e2e/
├── scripts/
│   ├── publish_test_result.py
│   └── wait_for_health.py
├── compose.yaml
├── Dockerfile
├── Makefile
├── pyproject.toml
├── alembic.ini
├── .env.example
├── .gitignore
└── README.md
```

This is a suggested organization, not a requirement to create a Python module for every table or operation. Keep the codebase compact and avoid abstractions that do not clarify ownership or transaction boundaries.

---

## 15. Architecture Decisions and Trade-offs

### ADR-001: Python and FastAPI

**Decision:** Use Python and FastAPI.

**Rationale:** The assignment permits Python or Go. Python enables fast implementation with typed validation, mature database libraries, and a strong testing ecosystem.

**Trade-off:** Async HTTP handling does not make blocking AMQP operations non-blocking. Broker clients must run in dedicated threads/processes or use a compatible asynchronous client with deliberate lifecycle management.

### ADR-002: PostgreSQL as the source of truth

**Decision:** Persist tenants, tasks, outbox events, and processed result IDs in PostgreSQL.

**Rationale:** The system needs durable state, transactions, uniqueness constraints, and concurrency control.

**Trade-off:** Database transactions and schema management require more setup than an in-memory store, but are essential to satisfy the assignment.

### ADR-003: Transactional outbox

**Decision:** Write the tenant mutation, task, and outbox event in one transaction; publish only from committed outbox rows.

**Rationale:** Avoids losing the logical task event when a process crashes between database commit and broker publication.

**Trade-off:** The relay adds operational and implementation complexity, and duplicates remain possible after ambiguous publisher outcomes. Idempotent processing is still required.

### ADR-004: Inbox-based result idempotency

**Decision:** Store worker-result `event_id` values with a unique constraint in the same transaction as the result's state changes.

**Rationale:** At-least-once broker delivery can repeat the same logical event. Database uniqueness gives a durable deduplication boundary.

**Trade-off:** The inbox grows over time. For this assignment, retain entries; a production system would need a retention policy that does not undermine the required deduplication window.

### ADR-005: Optimistic locking and conditional writes

**Decision:** Require the expected tenant version on PATCH and use a conditional database update or row locking within a transaction.

**Rationale:** Prevents lost updates when concurrent requests operate on the same tenant version.

**Trade-off:** Clients must handle version conflicts and re-read the current resource before retrying.

### ADR-006: RabbitMQ

**Decision:** Use RabbitMQ for task and result transport.

**Rationale:** The workload is command/task oriented and needs acknowledgments, redelivery, publisher confirms, and dead-lettering. RabbitMQ provides these capabilities without introducing a broader streaming platform.

**Trade-off:** It is not intended to provide Kafka-style event retention or replay as a general event log. Persisted task/outbox records remain the durable application record.

### ADR-007: One API process with managed messaging components

**Decision:** Keep API, outbox relay, and result consumer in one deployable control-plane application for the take-home, with the worker as a separate process.

**Rationale:** Meets the assignment's compact deployment requirement while keeping worker execution separate.

**Trade-off:** The API and messaging components share a deployment lifecycle and resource limits. If scaling or isolation requirements grow, split the relay and consumer into separate processes without changing their contracts.

### ADR-008: Docker Compose for local reproducibility

**Decision:** Use Docker Compose for the local system.

**Rationale:** The reviewer must start all dependencies with one command from a clean checkout.

**Trade-off:** Compose is a local development/test orchestration choice, not a claim that it is the production deployment platform.

---

## 16. Risks and Mitigations

| Risk                               | Mitigation                                                                    |
| ---------------------------------- | ----------------------------------------------------------------------------- |
| Slug race condition                | PostgreSQL unique constraint; map unique violation to `tenant_already_exists` |
| Two PATCH requests both succeed    | Conditional version update; database-backed concurrency test                  |
| Event lost after database commit   | Transactional outbox                                                          |
| Duplicate task publication         | Stable task ID; worker must tolerate redelivery                               |
| Duplicate result application       | Inbox table with unique `event_id`                                            |
| Stale result regresses state       | Task transition guards and terminal-state checks                              |
| Result applies to wrong tenant     | Verify result tenant ID matches persisted task and tenant                     |
| Poison message kills consumer      | Bounded retry and dead-letter queue                                           |
| Infinite retry loop                | Delayed retries, maximum attempt count, dead-letter policy                    |
| Blocking broker client stalls HTTP | Dedicated thread/process or async-compatible client                           |
| Startup races dependencies         | Compose health checks and bounded readiness waits                             |
| Tests pass only with mocks         | PostgreSQL and RabbitMQ integration tests                                     |
| Secrets committed accidentally     | `.env.example`, `.gitignore`, secret scanning if available                    |
| Overengineering consumes time      | Keep scope limited to requirements and documented trade-offs                  |

---

## 17. Acceptance Criteria

The architecture is considered implemented when all of the following are demonstrably true:

- [ ] A clean clone starts the entire stack with one documented command.
- [ ] The API and worker run as separate processes.
- [ ] PostgreSQL persists tenants and tasks across application restarts.
- [ ] Tenant slug validation and uniqueness match the specified contract.
- [ ] Destroyed tenant slugs remain reserved.
- [ ] Every tenant mutation increments the version.
- [ ] Two concurrent PATCH requests with the same version cannot both succeed.
- [ ] Tenant, task, and outbox records are written atomically.
- [ ] Outbound tasks are published only after the database transaction commits.
- [ ] Pending outbox events recover after relay/application restart.
- [ ] The worker simulates configurable delay and failure outcomes.
- [ ] The result consumer applies task and tenant state changes transactionally.
- [ ] Duplicate result delivery has no repeated side effects.
- [ ] Stale and out-of-order messages cannot regress terminal task state.
- [ ] Poison messages do not terminate the consumer.
- [ ] API errors use documented structured bodies and status codes.
- [ ] Tenant and task APIs follow the documented REST contract.
- [ ] The test command runs linting, formatting checks, type checks, security checks, and the automated suite.
- [ ] README documentation includes architecture, trade-offs, API examples, setup, and failure-reproduction commands.
- [ ] No secrets are committed.
- [ ] The public repository has a clean, reviewable commit history.

---

## 18. Implementation Order

Implement in this order to keep the highest-risk behavior visible early:

1. Project skeleton, configuration, Docker Compose, PostgreSQL, and RabbitMQ.
2. SQLAlchemy models and Alembic migrations.
3. Domain enums, state transitions, and unit tests.
4. Tenant/task repositories and database constraints.
5. Tenant and task HTTP endpoints with structured errors.
6. Optimistic locking and concurrency tests.
7. Transactional outbox and relay tests.
8. Worker simulator and outbound task consumption.
9. Result schema, inbox deduplication, and result consumer.
10. Retry, dead-letter, duplicate, and stale-message tests.
11. End-to-end lifecycle tests.
12. README, quality gates, cold-start verification, and public repository publication.

Do not begin by implementing every endpoint before deciding transaction boundaries. The transaction and idempotency contracts determine whether the system is correct under real failure conditions.

---

## 19. Open Implementation Details

The following are implementation choices to resolve and document during coding rather than reasons to delay the initial skeleton:

1. **Direct terminal result:** The task state machine requires `accepted → in_progress → done|failed`; terminal results received before `in_progress` should be treated as invalid or safely ignored unless the contract is clarified.
2. **DELETE concurrency:** The PRD explicitly requires version-based optimistic locking for update, not for delete. Define and test the DELETE race behavior.
3. **Invalid-message retry policy:** Choose a concrete maximum-attempt count and retry delay strategy; permanently malformed payloads should not retry forever.
4. **Outbox relay deployment:** Begin with one relay instance, but keep the implementation safe against duplicate publication and preferably safe for multiple relay instances.
5. **Task payload evolution:** Add a schema version or event type version if useful, while retaining all required fields.
6. **Worker redelivery:** A worker may repeat a task after a crash. The simulator must not rely on in-memory deduplication for control-plane correctness.
7. **Timestamp ordering:** Use `occurred_at` for diagnostics, not as the sole state-transition guard. Persisted task status and transactionally enforced transitions are authoritative.

---

## 20. Summary

The Tenant Provisioning Control Plane is a small asynchronous system with a demanding correctness contract. Its core architecture is intentionally conventional:

- FastAPI exposes tenant and task resources.
- PostgreSQL is the authoritative state store.
- Tenant lifecycle operations atomically write tenant state, task records, and outbox events.
- An outbox relay publishes committed task events to RabbitMQ.
- A separate worker simulator consumes tasks and publishes progress/results.
- A result consumer applies worker updates through an inbox-deduplicated database transaction.
- Database constraints and optimistic locking protect against races.
- Integration and concurrency tests demonstrate recovery, idempotency, and state-machine correctness.

The objective is not to maximize the number of components. It is to make the system's invariants explicit, implement them at the correct boundaries, and demonstrate that they continue to hold under concurrency, duplicate delivery, process crashes, and broker failures.
