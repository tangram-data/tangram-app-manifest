"""Tier-2 local OAuth: the full dance against a fake vendor, plus refresh."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tangram_app.local_connections import load_connection, save_connection
from tangram_app.local_oauth import (
    LocalOAuthError,
    ensure_fresh,
    oauth_connect,
    run_authorization,
    save_dev_client,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "minimal-connector"


class _TokenEndpoint(BaseHTTPRequestHandler):
    exchanges: list = []
    challenge: str | None = None
    auth_headers: list = []
    redirect_next: bool = False

    def do_POST(self):
        if _TokenEndpoint.redirect_next:
            _TokenEndpoint.redirect_next = False
            self.send_response(302)
            self.send_header("Location", "http://evil.example.net/token")
            self.end_headers()
            return
        _TokenEndpoint.auth_headers.append(self.headers.get("Authorization"))
        length = int(self.headers.get("Content-Length", "0"))
        form = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode()))
        _TokenEndpoint.exchanges.append(form)
        if form.get("grant_type") == "authorization_code":
            digest = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(form.get("code_verifier", "").encode()).digest()
                )
                .rstrip(b"=")
                .decode()
            )
            if form.get("code") != "fake-code" or digest != _TokenEndpoint.challenge:
                self._answer(400, {"error": "invalid_grant"})
                return
            self._answer(
                200,
                {
                    "access_token": "at-1",
                    "refresh_token": "rt-1",
                    "expires_in": 3600,
                    "scope": "email.read",
                },
            )
        elif form.get("grant_type") == "refresh_token" and form.get("refresh_token") == "rt-1":
            self._answer(200, {"access_token": "at-2", "expires_in": 3600})
        else:
            self._answer(400, {"error": "unsupported_grant_type"})

    def _answer(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _oauth_spec(token_url: str) -> dict:
    return {
        "authorizationUrl": "http://127.0.0.1:1/authorize",  # never fetched
        "tokenUrl": token_url,
        "scopes": ["email.read"],
        "pkce": True,
        "tokenAuthMethod": "ClientSecretPost",
        "refreshWindowSeconds": 600,
        "additionalAuthorizeParams": [{"name": "access_type", "value": "offline"}],
        "tenant": {"kind": "CallbackParam", "name": "realmId"},
    }


class LocalOAuthDanceTest(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        os.environ["TANGRAM_HOME"] = self._home.name
        self.addCleanup(os.environ.pop, "TANGRAM_HOME", None)
        _TokenEndpoint.exchanges = []
        _TokenEndpoint.challenge = None
        _TokenEndpoint.auth_headers = []
        _TokenEndpoint.redirect_next = False
        self.vendor = ThreadingHTTPServer(("127.0.0.1", 0), _TokenEndpoint)
        threading.Thread(target=self.vendor.serve_forever, daemon=True).start()
        self.addCleanup(self.vendor.shutdown)
        self.token_url = f"http://127.0.0.1:{self.vendor.server_port}/token"

    def _approve(self, url: str, extra: str = "&realmId=realm-7"):
        """Simulate the vendor redirecting the browser back to loopback."""
        parts = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.assertEqual(parts["response_type"], "code")
        self.assertEqual(parts["scope"], "email.read")
        self.assertEqual(parts["access_type"], "offline")
        self.assertEqual(parts["code_challenge_method"], "S256")
        _TokenEndpoint.challenge = parts["code_challenge"]
        callback = f"{parts['redirect_uri']}?state={parts['state']}&code=fake-code{extra}"
        with urllib.request.urlopen(callback, timeout=5) as response:
            self.assertEqual(response.status, 200)

    def test_full_dance_stores_connection_without_leaking_tokens(self):
        import shutil

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "echo"
            shutil.copytree(FIXTURE, root)
            spec_pkl = root / "manifests" / "api" / "spec.pkl"
            spec_pkl.write_text(
                spec_pkl.read_text(encoding="utf-8")
                + f'''
oauth = new Dynamic {{
  authorizationUrl = "http://127.0.0.1:1/authorize"
  tokenUrl = "{self.token_url}"
  scopes = List("email.read")
  pkce = true
  tokenAuthMethod = "ClientSecretPost"
  refreshWindowSeconds = 600
  additionalAuthorizeParams = List(new Dynamic {{ name = "access_type"; value = "offline" }})
  tenant = new Dynamic {{ kind = "CallbackParam"; name = "realmId" }}
}}
''',
                encoding="utf-8",
            )
            summary = oauth_connect(
                root,
                "com.example/echo",
                client_id="dev-client",
                client_secret="dev-secret",
                on_url=self._approve,
            )
        self.assertEqual(summary["tenant"], "realm-7")
        self.assertTrue(summary["refreshToken"])
        self.assertNotIn("at-1", json.dumps(summary))
        connection = load_connection("com.example/echo")
        self.assertEqual(connection["token"], "at-1")
        self.assertEqual(connection["refreshToken"], "rt-1")
        self.assertEqual(connection["tenant"], "realm-7")
        exchange = _TokenEndpoint.exchanges[-1]
        self.assertEqual(exchange["client_id"], "dev-client")
        self.assertEqual(exchange["client_secret"], "dev-secret")

    def test_state_mismatch_and_vendor_error_refuse(self):
        oauth = _oauth_spec(self.token_url)

        def wrong_state(url):
            parts = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            with urllib.request.urlopen(
                f"{parts['redirect_uri']}?state=WRONG&code=fake-code", timeout=5
            ):
                pass

        with self.assertRaises(LocalOAuthError) as caught:
            run_authorization(oauth, "dev-client", on_url=wrong_state, timeout_seconds=5)
        self.assertIn("state", str(caught.exception))

        def vendor_denied(url):
            parts = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            with urllib.request.urlopen(
                f"{parts['redirect_uri']}?state={parts['state']}&error=access_denied", timeout=5
            ):
                pass

        with self.assertRaises(LocalOAuthError) as caught:
            run_authorization(oauth, "dev-client", on_url=vendor_denied, timeout_seconds=5)
        self.assertIn("access_denied", str(caught.exception))

    def test_reserved_authorize_params_refuse(self):
        oauth = dict(
            _oauth_spec(self.token_url),
            additionalAuthorizeParams=[{"name": "state", "value": "evil"}],
        )
        opened = []
        with self.assertRaises(LocalOAuthError) as caught:
            run_authorization(oauth, "c", on_url=opened.append, timeout_seconds=1)
        self.assertIn("state", str(caught.exception))
        self.assertEqual(opened, [])  # refused before any URL was surfaced

    def test_token_endpoint_redirect_refuses(self):
        from tangram_app.local_oauth import _token_request

        _TokenEndpoint.redirect_next = True
        with self.assertRaises(LocalOAuthError) as caught:
            _token_request(
                _oauth_spec(self.token_url),
                {"grant_type": "refresh_token", "refresh_token": "rt-1"},
                {"clientId": "dev-client", "clientSecret": "s"},
            )
        self.assertIn("redirect", str(caught.exception))

    def test_client_secret_basic_form_encodes_components(self):
        from tangram_app.local_oauth import _token_request

        oauth = dict(_oauth_spec(self.token_url), tokenAuthMethod="ClientSecretBasic")
        _token_request(
            oauth,
            {"grant_type": "refresh_token", "refresh_token": "rt-1"},
            {"clientId": "id:with/odd chars", "clientSecret": "s%cr:t"},
        )
        header = _TokenEndpoint.auth_headers[-1]
        decoded = base64.b64decode(header.removeprefix("Basic ")).decode()
        self.assertEqual(decoded, "id%3Awith%2Fodd%20chars:s%25cr%3At")

    def test_https_required_for_remote_authorization_url(self):
        oauth = dict(_oauth_spec(self.token_url), authorizationUrl="http://vendor.example.com/auth")
        with self.assertRaises(LocalOAuthError):
            run_authorization(oauth, "c", on_url=lambda url: None, timeout_seconds=1)

    def test_ensure_fresh_refreshes_only_inside_window(self):
        oauth = _oauth_spec(self.token_url)
        spec = {"oauth": oauth}
        save_dev_client("com.example/echo", "dev-client", "dev-secret")
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        save_connection(
            "com.example/echo", "at-1", refresh_token="rt-1", expires_at=future
        )
        fresh = ensure_fresh(spec, "com.example/echo", load_connection("com.example/echo"))
        self.assertEqual(fresh["token"], "at-1")  # untouched, no HTTP
        self.assertEqual(_TokenEndpoint.exchanges, [])

        soon = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        save_connection(
            "com.example/echo", "at-1", refresh_token="rt-1", expires_at=soon
        )
        stale = load_connection("com.example/echo")  # still expiring soon, token at-1
        refreshed = ensure_fresh(spec, "com.example/echo", stale)
        self.assertEqual(refreshed["token"], "at-2")
        self.assertEqual(refreshed["refreshToken"], "rt-1")  # kept when not rotated
        self.assertEqual(_TokenEndpoint.exchanges[-1]["grant_type"], "refresh_token")

        # A racer holding the STALE snapshot re-reads under the lock and sees
        # the rotation instead of refreshing again.
        exchange_count = len(_TokenEndpoint.exchanges)
        again = ensure_fresh(spec, "com.example/echo", stale)
        self.assertEqual(again["token"], "at-2")
        self.assertEqual(len(_TokenEndpoint.exchanges), exchange_count)


if __name__ == "__main__":
    unittest.main()
