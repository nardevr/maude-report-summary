"""Shared test setup: tests must run offline, so any real network connection fails the test."""

import json
import socket
import urllib.parse
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise RuntimeError("network access attempted during tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self.content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()


NOT_FOUND = {"error": {"code": "NOT_FOUND", "message": "No matches found!"}}


class FakeSession:
    """Serves openFDA-shaped responses from a record list, or a fixed status, with no network.

    ``fail`` maps a query kind ("records", "count_date_received", "count_event_type") to an
    HTTP status (or an Exception instance) returned on every call of that kind.
    """

    def __init__(self, records: list[dict] | None, meta: dict | None = None, fail: dict | None = None):
        self.records = records or []
        self.meta = meta or {"last_updated": "2026-10-13"}
        self.fail = fail or {}
        self.calls: list[str] = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        kind = {"date_received": "count_date_received", "event_type.exact": "count_event_type"}.get(q.get("count"), "records")
        if kind in self.fail:
            f = self.fail[kind]
            if isinstance(f, Exception):
                raise f
            return FakeResponse(f, {"error": {"code": "SERVER_ERROR", "message": "simulated"}})
        if not self.records:
            return FakeResponse(404, NOT_FOUND)
        if kind == "count_date_received":
            days: dict[str, int] = {}
            for r in self.records:
                days[r["date_received"]] = days.get(r["date_received"], 0) + 1
            return FakeResponse(200, {"meta": self.meta, "results": [{"time": t, "count": c} for t, c in sorted(days.items())]})
        if kind == "count_event_type":
            ev: dict[str, int] = {}
            for r in self.records:
                ev[r["event_type"]] = ev.get(r["event_type"], 0) + 1
            return FakeResponse(200, {"meta": self.meta, "results": [{"term": t, "count": c} for t, c in ev.items()]})
        skip, limit = int(q.get("skip", 0)), int(q["limit"])
        meta = dict(self.meta, results={"skip": skip, "limit": limit, "total": len(self.records)})
        return FakeResponse(200, {"meta": meta, "results": self.records[skip: skip + limit]})
