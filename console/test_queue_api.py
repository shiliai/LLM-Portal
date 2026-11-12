import asyncio
import hashlib
import json
from pathlib import Path

from starlette.testclient import TestClient

from live_requests import LiveRequests
from testutil import install_litellm_stub, load_service

MASTER = "sk-queue-unit-master"
KEY = "sk-queue-unit-caller"
XRW = {"X-Requested-With": "XMLHttpRequest"}


def load_console(tmp_path):
    return load_service(Path(__file__).parent / "console.py", {
        "CONSOLE_DATA": str(tmp_path / "console"), "LIVE_REQUESTS_DB": str(tmp_path / "live.db"),
        "LITELLM_MASTER_KEY": MASTER, "ONBOARD_ADMIN_TOKEN": "test",
        "LITELLM_BASE": "http://litellm.invalid", "ADMIN_EMAIL": "", "SESSION_COOKIE_SECURE": "false"})


def handler(method, path, bearer, body):
    if path == "/global/spend":
        return (200, {}) if bearer == MASTER else (401, {})
    if path == "/key/list":
        return 200, {"keys": [{"token": hashlib.sha256(KEY.encode()).hexdigest(), "key_alias": "unit-caller", "metadata": {"group": "dev"}}]}
    if path == "/key/info":
        return 200, {"info": {"key_alias": "unit-caller", "blocked": False}}
    return 200, {}


def test_admin_view_live_metadata_filters_and_no_secrets(tmp_path, monkeypatch):
    install_litellm_stub(monkeypatch, handler)
    mod = load_console(tmp_path)
    tracker = LiveRequests(tmp_path / "live.db")

    async def prepare():
        for i in range(12):
            rid = await tracker.begin({"headers": [(b"authorization", ("Bearer " + KEY).encode())],
                                       "path": "/v1/chat/completions", "client": ("192.0.2.1", 9)})
            await tracker.describe(rid, model="m" + str(i), streaming=True)
        await tracker.flush()
    asyncio.run(prepare())
    with TestClient(mod.app) as client:
        assert client.get("/console/api/requests/live").status_code == 401
        assert client.get("/console/api/requests/backends").status_code == 401
        assert client.post("/console/api/login", json={"key": KEY}, headers=XRW).status_code == 200
        assert client.get("/console/api/requests/live").status_code == 403
        assert client.get("/console/api/requests/backends").status_code == 403
        client.post("/console/api/logout", headers=XRW)
        assert client.post("/console/api/login", json={"key": MASTER}, headers=XRW).status_code == 200
        response = client.get("/console/api/requests/live")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        payload = response.json()
        assert payload["available"] and payload["coverage"] == "compat_only"
        assert len(payload["requests"]) == 12
        assert all(r["alias"] == "unit-caller" and r["group"] == "dev" for r in payload["requests"])
        assert KEY not in response.text and MASTER not in response.text
        assert "key_hash" not in response.text and "payload" not in response.text
        assert client.post("/console/api/requests/live", headers=XRW).status_code == 405


def test_unavailable_tracker_is_not_an_empty_healthy_queue(tmp_path, monkeypatch):
    install_litellm_stub(monkeypatch, handler)
    mod = load_console(tmp_path)
    with TestClient(mod.app) as client:
        client.post("/console/api/login", json={"key": MASTER}, headers=XRW)
        assert client.get("/console/api/requests/live").json()["available"] is False


def test_backend_aliases_and_unavailable_metrics(tmp_path, monkeypatch):
    mod = load_console(tmp_path)
    calls = []

    def upstream(method, path, bearer, body):
        calls.append(path)
        if path == "/model/info":
            return 200, {"data": [{"model_name": name, "litellm_params": {"api_base": base}}
                                  for name, base in [("one", "http://192.0.2.1:8890/v1"),
                                                     ("alias", "http://192.0.2.1:8890/v1/"),
                                                     ("offline", "http://192.0.2.2:8890/v1"),
                                                     ("unregistered", "http://127.0.0.1:1234/v1"),
                                                     ("metadata", "http://169.254.169.254/v1"),
                                                     ("arbitrary-path", "http://192.0.2.1:8890/admin/private")]]}
        if path == "/onboard/admin/list":
            return 200, {"sites": [{"name": "one", "wg_ip": "192.0.2.1"}, {"name": "two", "wg_ip": "192.0.2.2"}]}
        if path == "/metrics":
            return 200, None
        return handler(method, path, bearer, body)

    install_litellm_stub(monkeypatch, upstream)
    # Parser checks independently measured TensorFold alias scenario.
    assert mod.queue_gauges('tensorfold:requests_running 3\ntensorfold:num_requests_running 3\n'
                            'tensorfold:requests_waiting 14\ntensorfold:num_requests_waiting 14') == {"running": 3, "waiting": 14}
    assert mod.queue_gauges('vllm:num_requests_waiting{m="a"} 2\nvllm:num_requests_waiting{m="b"} 3\ntensorfold:requests_waiting 5') == {"running": None, "waiting": 5}
    assert mod.queue_gauges('vllm:num_requests_running NaN\nvllm:num_requests_waiting -1') == {"running": None, "waiting": None}
    with TestClient(mod.app) as client:
        client.post("/console/api/login", json={"key": MASTER}, headers=XRW)
        response = client.get("/console/api/requests/backends")
        assert response.status_code == 200
        backends = response.json()["backends"]
        assert len(backends) == 2 and calls.count("/metrics") == 2
        assert backends[0]["models"] == ["one", "alias"]
        assert all(b["running"] is None and b["waiting"] is None and not b["available"] for b in backends)


def test_site_registry_failure_is_not_no_registered_backends(tmp_path, monkeypatch):
    mod = load_console(tmp_path)
    calls = []

    def unavailable(method, path, bearer, body):
        calls.append(path)
        if path == "/model/info":
            return 200, {"data": []}
        if path == "/onboard/admin/list":
            return 503, {"error": "registry offline"}
        return handler(method, path, bearer, body)

    install_litellm_stub(monkeypatch, unavailable)
    with TestClient(mod.app) as client:
        client.post("/console/api/login", json={"key": MASTER}, headers=XRW)
        response = client.get("/console/api/requests/backends")
        assert response.status_code == 503
        assert "/metrics" not in calls
