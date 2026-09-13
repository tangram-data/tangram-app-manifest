"""Private two-message subprocess protocol used by the desktop supervisor.

No approval authority lives here: the supervisor owns its stdin, evaluates the
validated descriptor, obtains a native decision, and only then sends execute.
This worker reuses the standalone execution engine, not the public app-call
routing path. It never reads a caller-supplied approval flag in the request.
"""
import contextlib
import json
import sys


def main():
    from .cli_hub import _local_inspect
    from .cli import _run, _error_details
    from .cli_parser import build_parser
    from .schema import validate

    output = sys.stdout
    def emit(value):
        output.write(json.dumps(value, separators=(",", ":")) + "\n")
        output.flush()
    locks = []
    try:
        line = sys.stdin.readline(1048577)
        if len(line) > 1048576:
            raise ValueError("invocation input exceeds 1 MiB")
        request = json.loads(line)
        with contextlib.redirect_stdout(sys.stderr):
            entry, app, action, binding = _local_inspect(request["action"], request["contract"])
            validate(request["input"], binding.input_schema)
        connector = app.graph.runtime_requirements.get("backend") == "connector"
        if not connector:
            from pathlib import Path
            from .local_runtime import _ProjectLock, project_lock_path, _safe_preview_dir
            root = Path(entry["root"])
            locks.append(_ProjectLock.acquire(project_lock_path(root)))
            if not (root / "manifests").is_dir():
                raise ValueError("app package was removed before invocation")
            preview = _safe_preview_dir(root)
            locks.append(_ProjectLock.acquire(preview / "runtime.lock"))
        emit({"state": "prepared", "action": binding.id, "contract": app.graph.package.digest,
              "effect": action.effect.value, "requiresConfirmation": action.requires_confirmation,
              "input": request["input"], "root": entry["root"], "connector": connector,
              "resourceType": action.resource_type, "actionName": action.name, "operationId": binding.operation_id})
        decision = json.loads(sys.stdin.readline(4096))
        if decision != {"execute": request["invocationId"]}:
            raise ValueError("supervisor did not authorize this invocation")
        # Re-load and recheck the digest after the human wait. The old call
        # engine also pins the digest at its own load/dispatch boundary.
        with contextlib.redirect_stdout(sys.stderr):
            entry, app, action, binding = _local_inspect(request["action"], request["contract"])
            connector = app.graph.runtime_requirements.get("backend") == "connector"
            if not connector:
                emit({"state": "validated"})
                # Keep both lifecycle locks until the protected Scala backend
                # and DB are stopped, including throughout the human wait.
                if json.loads(sys.stdin.readline(4096)) != {"finished": request["invocationId"]}:
                    raise ValueError("supervisor disconnected during execution")
                return 0
            from .supervisor_identity import authenticated_supervisor
            if not authenticated_supervisor():
                raise ValueError("Connector execution requires the signed Tangram supervisor as parent")
            args = ["call", entry["root"], binding.id, "--connected" if connector else "--local",
                    "--package-digest", request["contract"], "--input-json", json.dumps(request["input"]),
                    "--allow-mutation", "--confirm", "--timeout", str(request["timeoutSeconds"])]
            result = _run(build_parser().parse_args(args))
        emit({"state": "succeeded", "result": result})
    except Exception as error:
        code, _, message = _error_details(error)
        emit({"state": "failed", "error": {"code": code, "message": message}})
    finally:
        for lock in reversed(locks):
            lock.close()
    return 0
