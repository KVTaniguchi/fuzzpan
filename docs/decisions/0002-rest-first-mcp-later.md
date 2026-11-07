# 0002 — One REST API; MCP as a thin wrapper later

**Status:** Accepted (2026-11-02). Amended: requests now carry bearer tokens, not signatures.

## Context

A coding agent could connect via MCP. Planning agents and retailer watchers
can't necessarily speak MCP, but they can all make HTTP requests. The design calls for
one API that every agent uses identically.

## Decision

The board exposes a single REST/JSON API authenticated by per-agent bearer
tokens. A small client library wraps it. An MCP server for coding agents sits
on top of it, calls the same REST API with that agent's own token and adds no
extra privileges.

## Consequences

- Permission logic lives in exactly one place: the board server.
- The MCP wrapper is optional convenience. It can be removed without changing
  the board.
