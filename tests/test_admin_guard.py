from __future__ import annotations

from fastapi.testclient import TestClient

from admin_test_utils import build_admin_test_client


def test_admin_localhost_allowed():
    client, _session_local = build_admin_test_client()
    resp = client.get("/api/v1/admin/users")
    assert resp.status_code == 200


def test_admin_non_localhost_blocked():
    _local_client, _session_local = build_admin_test_client()
    # Build another client with a non-loopback remote host.
    app = _local_client.app
    client = TestClient(app, client=("8.8.8.8", 5555))
    resp = client.get("/api/v1/admin/users")
    assert resp.status_code == 403
