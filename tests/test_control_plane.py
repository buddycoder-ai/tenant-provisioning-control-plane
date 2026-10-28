from fastapi.testclient import TestClient

from app.db import Task, Tenant, make_session_factory
from app.main import create_app
from app.schemas import ResultEvent
from app.services import apply_result


def client(tmp_path):
    app = create_app(f"sqlite+pysqlite:///{tmp_path}/test.db")
    return TestClient(app), app


def test_create_duplicate_and_validation(tmp_path):
    test_client, _ = client(tmp_path)
    with test_client:
        assert (
            test_client.post("/v1/tenants", json={"slug": "alpha-one", "name": "Alpha"}).status_code
            == 201
        )
        duplicate = test_client.post("/v1/tenants", json={"slug": "alpha-one", "name": "Again"})
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == "tenant_already_exists"
        assert test_client.post("/v1/tenants", json={"slug": "bad", "name": " "}).status_code == 422


def test_result_lifecycle_is_idempotent_and_order_tolerant(tmp_path):
    test_client, _ = client(tmp_path)
    with test_client:
        created = test_client.post(
            "/v1/tenants", json={"slug": "alpha-one", "name": "Alpha"}
        ).json()
        tenant, task = created["tenant"], created["task"]
        # The app uses this same sqlite URL, so derive the database from the test path.
        _, factory = make_session_factory(f"sqlite+pysqlite:///{tmp_path}/test.db")
        with factory() as session:
            assert apply_result(
                session,
                ResultEvent(
                    event_id="one", task_id=task["id"], tenant_id=tenant["id"], status="in_progress"
                ),
            )
        with factory() as session:
            assert apply_result(
                session,
                ResultEvent(
                    event_id="two", task_id=task["id"], tenant_id=tenant["id"], status="done"
                ),
            )
        with factory() as session:
            assert not apply_result(
                session,
                ResultEvent(
                    event_id="three",
                    task_id=task["id"],
                    tenant_id=tenant["id"],
                    status="in_progress",
                ),
            )
            assert session.get(Task, task["id"]).status == "done"
            assert session.get(Tenant, tenant["id"]).status == "active"
            assert session.get(Tenant, tenant["id"]).version == 2


def test_patch_requires_active_and_conflicts(tmp_path):
    test_client, _ = client(tmp_path)
    with test_client:
        created = test_client.post(
            "/v1/tenants", json={"slug": "alpha-one", "name": "Alpha"}
        ).json()
        tenant, task = created["tenant"], created["task"]
        _, factory = make_session_factory(f"sqlite+pysqlite:///{tmp_path}/test.db")
        for event_id, status in (("one", "in_progress"), ("two", "done")):
            with factory() as session:
                apply_result(
                    session,
                    ResultEvent(
                        event_id=event_id, task_id=task["id"], tenant_id=tenant["id"], status=status
                    ),
                )
        ok = test_client.patch(f"/v1/tenants/{tenant['id']}", json={"name": "Beta", "version": 2})
        assert ok.status_code == 202
        conflict = test_client.patch(
            f"/v1/tenants/{tenant['id']}", json={"name": "Other", "version": 2}
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] in {
            "tenant_version_conflict",
            "tenant_update_not_allowed",
        }
