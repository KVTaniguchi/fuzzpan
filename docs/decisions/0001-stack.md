# 0001 — Stack: Python, FastAPI, SQLite

**Status:** Accepted (2026-11-02). Amended: PyNaCl and request signing were later dropped for bearer tokens.

## Context

The board is small, needs to run locally first, and must be cheap to host
later. It needs a small relational model, a JSON API and one simple HTML page. No vendor lock-in.

## Options considered

- **Python + FastAPI + SQLite.** Easy to test with pytest, single-file database, server-rendered HTML via Jinja.
- **TypeScript + Hono + SQLite.** Equally capable; slightly
  more tooling to set up.

## Decision

Python 3.12+, FastAPI, SQLite (stdlib `sqlite3`, no ORM),
Jinja2 templates for the feed page, pytest for tests. Dependencies managed
with `uv`.

## Consequences

- The whole board is one process plus one `.db` file. Easy to back up and move.
- Runs on any cheap VPS or small hosted container later. Nothing is
  provider-specific.
