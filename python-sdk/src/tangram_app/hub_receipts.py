"""Durable client requests for explicit resume; never an approval authority."""
import json
from .hub_journal import connection
from .hub_target import HubError

FIELDS = ("target_ref", "action", "contract", "input_json", "invocation_id", "timeout", "allow_mutation", "approve")


def get(invocation_id):
    with connection() as db:
        db.execute("CREATE TABLE IF NOT EXISTS client_request (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        row = db.execute("SELECT payload FROM client_request WHERE id = ?", (invocation_id,)).fetchone()
    return json.loads(row[0]) if row else None


def save(args):
    request = {field: getattr(args, field) for field in FIELDS}
    with connection() as db:
        db.execute("CREATE TABLE IF NOT EXISTS client_request (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT payload FROM client_request WHERE id = ?", (args.invocation_id,)).fetchone()
        if old:
            previous = json.loads(old[0])
            for field in ("target_ref", "action", "contract", "input_json"):
                if previous[field] != request[field]:
                    raise HubError("invocation_conflict", "This invocation ID already belongs to a different frozen request.")
            # Consent flags are caller intent, not an approval grant. Preserve
            # the latest explicit intent for this same frozen request so resume
            # cannot silently restore older flags after a pending approval.
            previous.update({field: request[field] for field in ("approve", "allow_mutation", "timeout")})
            db.execute("UPDATE client_request SET payload = ? WHERE id = ?", (json.dumps(previous), args.invocation_id))
        else:
            db.execute("INSERT INTO client_request VALUES (?, ?)", (args.invocation_id, json.dumps(request)))
