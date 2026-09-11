"""Local diagnostic invocation journal. A claim never expires into a retry.

An interrupted running call requires reconciliation: a process death cannot tell
us whether an upstream mutation committed. SQLite serializes competing claims.
This is a local-user boundary, not protection from other processes of that user.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager

from .hub_target import HubError, digest
from .local_store import tangram_home


@contextmanager
def connection():
    folder = tangram_home() / "hub"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / "invocations.sqlite3"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    db = sqlite3.connect(path, timeout=30)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS invocation (id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL)")
        yield db
        db.commit()
    finally:
        db.close()


def get(invocation_id: str, target: dict) -> dict:
    with connection() as db:
        row = db.execute("SELECT payload FROM invocation WHERE id = ?", (invocation_id,)).fetchone()
    if row is None:
        raise HubError("invocation_not_found", "no invocation with this ID in the selected target")
    result = json.loads(row[0])
    if result["target"] != target:
        raise HubError("invocation_not_found", "no invocation with this ID in the selected target")
    return result


def claim(invocation_id: str, request: dict, *, gated: bool, approved: bool) -> tuple[dict, bool]:
    request_hash = digest(request)
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT request_hash, state, payload FROM invocation WHERE id = ?", (invocation_id,)).fetchone()
        if row:
            if row[0] != request_hash:
                raise HubError("idempotency_conflict", "invocation ID was already used for another target, contract, action or input")
            record = json.loads(row[2])
            if row[1] != "pending_approval" or not approved:
                return record, False
        else:
            record = {"invocationId": invocation_id, "target": request["target"], "action": request["action"],
                      "contract": request["contract"], "inputHash": digest(request["input"])}
        record["state"] = "pending_approval" if gated and not approved else "running"
        if approved:
            record["approval"] = {"principal": "local-user", "kind": "explicit_cli_consent"}
        db.execute("INSERT OR REPLACE INTO invocation VALUES (?, ?, ?, ?)",
                   (invocation_id, request_hash, record["state"], json.dumps(record)))
        return record, record["state"] == "running"


def complete(record: dict, state: str, **fields) -> dict:
    result = {**record, **fields, "state": state}
    with connection() as db:
        changed = db.execute("UPDATE invocation SET state = ?, payload = ? WHERE id = ? AND state = 'running'",
                             (state, json.dumps(result), record["invocationId"])).rowcount
        if changed != 1:
            raise HubError("invocation_conflict", "invocation is no longer running")
    return result
