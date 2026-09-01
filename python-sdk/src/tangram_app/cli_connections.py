"""CLI handlers for developer connections (connect / disconnect / --connected).

Split from cli.py to keep the command shell small; these return the same
envelope-data dicts the other command branches do.
"""

from __future__ import annotations

import asyncio
import sys

from .local_connections import (
    delete_connection,
    load_connection,
    load_connector_spec,
    render_connection,
    save_connection,
)
from .local_oauth import ensure_fresh, oauth_connect


class ConnectionArgumentsError(ValueError):
    """Raised for caller-actionable connect/call argument problems."""


def handle_connect(args, root, app_id: str) -> dict:
    if args.oauth:
        secret = args.client_secret
        if secret == "-":
            secret = sys.stdin.read().strip()
        if args.no_browser:
            def on_url(url: str) -> None:
                print(f"Open to authorize: {url}", file=sys.stderr)
        else:
            import webbrowser

            on_url = webbrowser.open
        summary = oauth_connect(
            root,
            app_id,
            client_id=args.client_id,
            client_secret=secret,
            on_url=on_url,
            timeout_seconds=args.oauth_timeout,
        )
        return {"connected": app_id, "mode": "oauth", **summary}
    token = sys.stdin.read() if args.token == "-" else args.token
    load_connector_spec(root)  # refuse non-connector packages up front
    stored = save_connection(app_id, token, tenant=args.tenant)
    return {"connected": app_id, "mode": "token", "connection": str(stored)}


def handle_disconnect(app_id: str) -> dict:
    return {"disconnected": app_id, "removed": delete_connection(app_id)}


def connected_call(args, app, root, arguments, *, policy=None) -> dict:
    spec = load_connector_spec(root)
    connection = load_connection(app.graph.package.id)
    if connection is None:
        raise ConnectionArgumentsError(
            f"no developer connection for {app.graph.package.id}; run "
            f"`tangram-app connect {args.target} --oauth` (or --token ...) first"
        )
    connection = ensure_fresh(spec, app.graph.package.id, connection)
    endpoint, auth_headers = render_connection(
        spec, connection, endpoint_override=args.endpoint
    )
    bound = app.bind(
        backend=endpoint,
        headers=auth_headers,
        policy=policy,
        audit_path=args.audit_path,
        timeout_seconds=args.timeout,
        allow_remote=True,
    )
    result = asyncio.run(bound.call(args.binding, arguments))
    binding_id = bound.graph.resolve(args.binding)[1].id
    return {"bindingId": binding_id, "result": result, "endpoint": endpoint}
