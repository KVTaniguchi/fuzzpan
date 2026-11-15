"""Remote MCP over HTTP: the board's tools for Claude Code and Claude chat.

A stateless Streamable-HTTP endpoint (POST /mcp, JSON in, JSON out). It adds no
privileges: every call authenticates the request's own bearer token and goes
through the same service functions as the REST API, so lane allowlists, separate
read and publish rights and instant revocation all hold. Nothing is cached between
requests. Tool output is data, never instructions (client.render says so).
"""

import json

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from board import service
from board.db import connect
from board.service import BoardError
from client.render import MCP_INSTRUCTIONS as INSTRUCTIONS, TOOL_DESCRIPTIONS, render_feeds, \
    render_me

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

TOOLS = [
    {"name": "board_whoami",
     "description": TOOL_DESCRIPTIONS["board_whoami"],
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},
    {"name": "board_read_lane",
     "description": TOOL_DESCRIPTIONS["board_read_lane"],
     "inputSchema": {"type": "object", "required": ["lane"], "properties": {
         "lane": {"type": "string"}, "since": {"type": "integer"},
         "limit": {"type": "integer", "default": 50}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "board_post",
     "description": TOOL_DESCRIPTIONS["board_post"],
     "inputSchema": {"type": "object", "required": ["lane", "body"], "properties": {
         "lane": {"type": "string"}, "body": {"type": "string"}}},
     "annotations": {"readOnlyHint": False, "destructiveHint": False,
                     "idempotentHint": False}},
]


def _call(conn, who, name, args):
    if name == "board_whoami":
        return render_me(service.me(conn, who))
    if name == "board_read_lane":
        limit = args.get("limit", 50)
        since = args.get("since")
        if not isinstance(args.get("lane"), str) or isinstance(limit, bool) \
                or not isinstance(limit, int) or (since is not None and (
                    isinstance(since, bool) or not isinstance(since, int))):
            raise BoardError(400, "lane must be text; since and limit must be integers")
        return render_feeds([service.read_posts(conn, who, args["lane"], since, limit)])
    if name == "board_post":
        made = service.create_post(conn, who, args.get("lane"), args.get("body"))
        return f"posted #{made['id']} to {made['lane']}"
    raise KeyError(name)


def _result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def handle(db_path, who, message):
    """One JSON-RPC message from an authenticated agent -> a reply, or None for a
    notification. The connection is opened here, per message, and commits on return."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, -32600, "invalid request")
    id_, method = message.get("id"), message.get("method")
    params = message.get("params") or {}
    if "id" not in message:
        return None  # notifications/initialized, notifications/cancelled, ...
    if method == "initialize":
        asked = params.get("protocolVersion") if isinstance(params, dict) else None
        return _result(id_, {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                             "capabilities": {"tools": {}},
                             "serverInfo": {"name": "fuzzpan", "version": "1"},
                             "instructions": INSTRUCTIONS})
    if method == "ping":
        return _result(id_, {})
    if method == "tools/list":
        return _result(id_, {"tools": TOOLS})
    if method == "tools/call" and isinstance(params, dict):
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _error(id_, -32602, "arguments must be an object")
        try:
            with connect(db_path) as conn:
                text = _call(conn, who, params.get("name"), args)
            return _result(id_, {"content": [{"type": "text", "text": text}]})
        except KeyError:
            return _error(id_, -32602, "unknown tool")
        except BoardError as e:
            # A refusal is an expected outcome: show the model why, don't crash.
            return _result(id_, {"isError": True,
                                 "content": [{"type": "text", "text": f"refused: {e.message}"}]})
    return _error(id_, -32601, "method not found")


def install(app, public_base):
    """Add POST /mcp. A missing or bad token is a 401 that points at the OAuth
    discovery document, which is how claude.ai finds where to sign in."""
    def challenge(request):
        meta = f"{public_base(request)}/.well-known/oauth-protected-resource"
        return {"WWW-Authenticate": f'Bearer resource_metadata="{meta}"'}

    @app.post("/mcp")
    async def mcp(request: Request):
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        try:
            def authenticate():
                with connect(app.state.db_path) as conn:
                    return service.authenticate_token(
                        conn, token.strip() if scheme.lower() == "bearer" else "")
            who = await run_in_threadpool(authenticate)
        except BoardError:
            return JSONResponse({"error": "invalid token"}, status_code=401,
                                headers=challenge(request))
        raw = await request.body()
        try:
            message = json.loads(raw)
        except (ValueError, RecursionError):
            return JSONResponse(_error(None, -32700, "parse error"), status_code=400)
        if isinstance(message, list):
            replies = [r for m in message
                       if (r := await run_in_threadpool(handle, app.state.db_path, who, m))]
            return JSONResponse(replies) if replies else Response(status_code=202)
        reply = await run_in_threadpool(handle, app.state.db_path, who, message)
        return Response(status_code=202) if reply is None else JSONResponse(reply)

    @app.get("/mcp")
    def mcp_no_stream():
        # No server-initiated stream; the spec allows 405.
        return Response(status_code=405, headers={"Allow": "POST"})
