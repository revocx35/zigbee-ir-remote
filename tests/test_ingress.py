import asyncio

from aiohttp.test_utils import TestClient, TestServer

import main


async def _get(remote, path, method="GET"):
    async with TestClient(TestServer(main.make_app(remote))) as client:
        res = await client.request(method, path)
        return res.status


def test_ui_api_refuses_other_containers_when_supervised(remote, monkeypatch):
    monkeypatch.setattr(main, "supervisor_token", lambda: "tok")
    # The test client connects from 127.0.0.1, not from the ingress proxy.
    assert asyncio.run(_get(remote, "/api/state")) == 403
    assert asyncio.run(_get(remote, "/api/devices/x/controls/y/send", "POST")) == 403


def test_ui_api_open_in_standalone_mode(remote, monkeypatch):
    monkeypatch.setattr(main, "supervisor_token", lambda: None)
    assert asyncio.run(_get(remote, "/api/state")) == 200
