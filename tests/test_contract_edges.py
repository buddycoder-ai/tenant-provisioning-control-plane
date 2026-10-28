from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import InboxEvent, OutboxEvent, Task, make_session_factory
from app.main import create_app
from app.messaging import retry_delay_seconds
from app.schemas import ResultEvent
from app.services import DomainError, apply_result, request_update


def make_client(tmp_path):
    database_url = f"sqlite+pysqlite:///{tmp_path}/contract.db"
    return TestClient(create_app(database_url)), database_url


def create(test_client: TestClient, slug: str = "contract-one") -> dict:
    response = test_client.post("/v1/tenants", json={"slug": slug, "name": "Contract"})
    assert response.status_code == 201
    return response.json()


def complete(factory, tenant: dict, task: dict, prefix: str = "result") -> None:
    for event_id, status in ((f"{prefix}-started", "in_progress"), (f"{prefix}-done", "done")):
        with factory() as session:
            assert apply_result(
                session,
                ResultEvent(
                    event_id=event_id,
                    task_id=task["id"],
                    tenant_id=tenant["id"],
                    status=status,
                ),
            )


def test_create_writes_task_and_outbox_atomically(tmp_path):
    test_client, database_url = make_client(tmp_path)
    with test_client:
        created = create(test_client)
        _, factory = make_session_factory(database_url)
        with factory() as session:
            task = session.get(Task, created["task"]["id"])
            event = session.scalar(select(OutboxEvent).where(OutboxEvent.task_id == task.id))
            assert task.status == "accepted"
            assert event.payload["id"] == task.id
            assert event.payload["tenant_id"] == created["tenant"]["id"]
            assert event.payload["type"] == "deploy"
            assert event.published_at is None


def test_result_rejects_mismatch_and_requires_progress_first(tmp_path):
    test_client, database_url = make_client(tmp_path)
    with test_client:
        created = create(test_client)
        tenant, task = created["tenant"], created["task"]
        _, factory = make_session_factory(database_url)
        with factory() as session:
            assert not apply_result(
                session,
                ResultEvent(
                    event_id="direct-done",
                    task_id=task["id"],
                    tenant_id=tenant["id"],
                    status="done",
                ),
            )
            assert session.get(Task, task["id"]).status == "accepted"
            assert session.get(InboxEvent, "direct-done") is not None
        with factory() as session, pytest.raises(DomainError, match="unknown or mismatched"):
            apply_result(
                session,
                ResultEvent(
                    event_id="mismatch",
                    task_id=task["id"],
                    tenant_id="other-tenant",
                    status="in_progress",
                ),
            )


def test_task_endpoints_filters_and_not_found_errors(tmp_path):
    test_client, _ = make_client(tmp_path)
    with test_client:
        created = create(test_client)
        listed = test_client.get("/v1/tasks", params={"tenant_id": created["tenant"]["id"]})
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [created["task"]["id"]]
        assert test_client.get("/v1/tasks/missing").json()["error"]["code"] == "task_not_found"
        assert test_client.get("/v1/tenants/missing").json()["error"]["code"] == "tenant_not_found"


def test_destroy_is_allowed_after_failure_and_terminal_update_maps_to_failed(tmp_path):
    test_client, database_url = make_client(tmp_path)
    with test_client:
        created = create(test_client)
        tenant, deploy = created["tenant"], created["task"]
        _, factory = make_session_factory(database_url)
        complete(factory, tenant, deploy, "deploy")
        update = test_client.patch(
            f"/v1/tenants/{tenant['id']}", json={"name": "Changed", "version": 2}
        ).json()
        with factory() as session:
            assert apply_result(
                session,
                ResultEvent(
                    event_id="update-started",
                    task_id=update["task"]["id"],
                    tenant_id=tenant["id"],
                    status="in_progress",
                ),
            )
        with factory() as session:
            assert apply_result(
                session,
                ResultEvent(
                    event_id="update-failed",
                    task_id=update["task"]["id"],
                    tenant_id=tenant["id"],
                    status="failed",
                ),
            )
        destroy = test_client.delete(f"/v1/tenants/{tenant['id']}")
        assert destroy.status_code == 202
        assert destroy.json()["tenant"]["status"] == "destroying"


def test_only_one_concurrent_patch_can_succeed(tmp_path):
    test_client, database_url = make_client(tmp_path)
    with test_client:
        created = create(test_client)
        _, factory = make_session_factory(database_url)
        complete(factory, created["tenant"], created["task"])

    def update_once():
        with factory() as session:
            try:
                request_update(session, created["tenant"]["id"], "Raced", 2)
                return "success"
            except DomainError as exc:
                return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: update_once(), range(2)))
    assert outcomes.count("success") == 1
    assert (
        outcomes.count("tenant_version_conflict") + outcomes.count("tenant_update_not_allowed") == 1
    )


def test_database_rejects_second_open_task(tmp_path):
    test_client, database_url = make_client(tmp_path)
    with test_client:
        created = create(test_client)
        _, factory = make_session_factory(database_url)
        with factory() as session:
            session.add(Task(type="deploy", tenant_id=created["tenant"]["id"], status="accepted"))
            with pytest.raises(IntegrityError):
                session.commit()


def test_outbox_backoff_is_exponential_and_bounded():
    assert retry_delay_seconds(1) == 0.25
    assert retry_delay_seconds(2) == 0.5
    assert retry_delay_seconds(3) == 1.0
    assert retry_delay_seconds(100) == 30.0
