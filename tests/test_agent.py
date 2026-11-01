import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

import main


def _agent(remote):
    agent = main.AgentAPI(remote, main.AgentTLS(remote.store.path.parent / "tls"))
    remote.store.data["agent_api"] = {"token_hash": main.token_hash("irr_good")}
    return agent


async def _post(agent, body, chunks=1):
    async with TestClient(TestServer(agent.app())) as client:
        async def gen():
            step = -(-len(body) // chunks)
            for i in range(0, len(body), step):
                yield body[i:i + step]
                await asyncio.sleep(0.01)
        data = gen() if chunks > 1 else body
        res = await client.post("/mcp", data=data, headers={"Authorization": "Bearer irr_good"})
        return res.status, await res.json()


def test_body_sent_in_several_chunks_is_read_whole(remote):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"pad": "x" * 20000}}
    status, out = asyncio.run(_post(_agent(remote), json.dumps(msg).encode(), chunks=4))
    assert status == 200 and out["result"] == {}


def test_params_that_are_not_an_object_are_refused(remote):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": ["x"]}
    status, out = asyncio.run(_post(_agent(remote), json.dumps(msg).encode()))
    assert status == 400 and out["error"]["code"] == -32602


def test_bad_token_is_401(remote):
    agent = _agent(remote)

    async def go():
        async with TestClient(TestServer(agent.app())) as client:
            res = await client.post("/mcp", data=b"{}", headers={"Authorization": "Bearer nope"})
            return res.status
    assert asyncio.run(go()) == 401
