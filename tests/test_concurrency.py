"""Overlapping requests must not stall each other.

Regression: async request code made blocking SQLite calls on the event loop.
A request waiting for the write lock froze the loop, so the request holding
the lock could never commit, which surfaced as "database is locked" 500s.
"""

import asyncio
import json

import httpx


def test_overlapping_reads_and_posts_all_succeed(board):
    agent = board.register("alice", "planner")
    board.grant("alice", "household", "planner")
    path = "/v1/lanes/household/posts"

    async def one(http, i):
        body = json.dumps({"body": f"post {i}"}).encode() if i % 2 else b""
        method = "POST" if body else "GET"
        headers = {"Authorization": f"Bearer {agent.token}"}
        if body:
            headers["Content-Type"] = "application/json"
        r = await http.request(method, path, content=body, headers=headers)
        return r.status_code

    async def main():
        transport = httpx.ASGITransport(app=board.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            return await asyncio.wait_for(
                asyncio.gather(*(one(http, i) for i in range(16))), timeout=30)

    statuses = asyncio.run(main())
    assert sorted(set(statuses)) == [200, 201], statuses
