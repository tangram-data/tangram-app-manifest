"""Loopback UI host for sandboxed React components in local Tangram apps."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from typing import TYPE_CHECKING, Any, Mapping
from urllib.parse import urlsplit

from .errors import LocalRuntimeError
from .policy import LocalDevelopmentPolicy

if TYPE_CHECKING:
    from .local_runtime import LocalAppSession


_COMPONENT_CATALOG = {
    "@ant-design/icons": "5",
    "antd": "5",
    "dayjs": "1",
    "esbuild": "0.27.7",
    "react": "18",
    "react-dom": "18",
    "recharts": "2",
}
_MAX_ACTION_BODY = 1024 * 1024
_MAX_BUNDLE = 12 * 1024 * 1024
_MAX_CSS = 512 * 1024


@dataclass(frozen=True, slots=True)
class UiBundle:
    javascript: bytes
    stylesheet: bytes | None


class UiConfirmationRequired(Exception):
    pass


class LocalUiServer:
    def __init__(
        self,
        server: ThreadingHTTPServer,
        thread: threading.Thread,
        session: "LocalAppSession",
    ) -> None:
        self._server = server
        self._thread = thread
        self._session = session
        self.url = f"http://127.0.0.1:{server.server_port}/"

    @classmethod
    def start(
        cls,
        session: "LocalAppSession",
        preview: Path,
        *,
        environment: Mapping[str, str],
        managed_environment: bool,
    ) -> "LocalUiServer | None":
        ui = session.app.ui()
        if not ui:
            return None
        if ui.get("kind") != "sandboxed":
            raise LocalRuntimeError("the declared root UI component is not sandboxed")
        entry = ui.get("entry")
        if not isinstance(entry, str) or not entry:
            raise LocalRuntimeError("the declared root UI component has no source entry")
        root = session.app.source_root
        if root is None:
            raise LocalRuntimeError("local UI serving requires a source package")
        ui_root = (root / "manifests/ui").resolve()
        entry_path = (ui_root / entry).resolve()
        components_root = (ui_root / "components").resolve()
        if not entry_path.is_file() or not entry_path.is_relative_to(components_root):
            raise LocalRuntimeError(f"UI entry {entry!r} is missing or outside components/")
        component_dir = entry_path.parent
        esbuild, node_modules = _resolve_toolchain(
            root,
            preview,
            environment,
            managed=managed_environment,
        )
        bundle = _compile_component(component_dir, entry_path, esbuild, node_modules)
        handler = _handler_factory(session, bundle)
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            server.daemon_threads = True
            thread = threading.Thread(
                target=server.serve_forever,
                name="tangram-local-ui",
                daemon=True,
            )
            thread.start()
            return cls(server, thread, session)
        except OSError as error:
            raise LocalRuntimeError(f"could not start local UI server: {error}") from error

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _resolve_toolchain(
    root: Path,
    preview: Path,
    environment: Mapping[str, str],
    *,
    managed: bool,
) -> tuple[Path, Path]:
    explicit_modules = environment.get("TANGRAM_COMPONENT_NODE_MODULES", "").strip()
    explicit_esbuild = environment.get("TANGRAM_ESBUILD_BIN", "").strip()
    if explicit_modules and explicit_esbuild:
        modules = Path(explicit_modules).expanduser().resolve()
        esbuild = Path(explicit_esbuild).expanduser().resolve()
        if modules.is_dir() and esbuild.is_file() and os.access(esbuild, os.X_OK):
            return esbuild, modules

    directory: Path | None = root
    hops = 0
    while directory is not None and hops < 6:
        for relative in ("component-deps/node_modules", "node_modules"):
            modules = directory / relative
            esbuild = modules / ".bin/esbuild"
            if modules.is_dir() and esbuild.exists() and os.access(esbuild, os.X_OK):
                return esbuild.resolve(), modules.resolve()
        directory = directory.parent
        hops += 1

    path_esbuild = shutil.which("esbuild", path=environment.get("PATH"))
    if path_esbuild and explicit_modules and Path(explicit_modules).is_dir():
        return Path(path_esbuild).resolve(), Path(explicit_modules).resolve()
    if not managed:
        raise LocalRuntimeError(
            "esbuild/component dependencies are unavailable; set "
            "TANGRAM_ESBUILD_BIN and TANGRAM_COMPONENT_NODE_MODULES"
        )
    return _ensure_managed_toolchain(preview, environment)


def _ensure_managed_toolchain(
    preview: Path, environment: Mapping[str, str]
) -> tuple[Path, Path]:
    npm = shutil.which("npm", path=environment.get("PATH"))
    if npm is None:
        raise LocalRuntimeError(
            "the UI runtime needs npm for its first setup; install Node.js or set "
            "TANGRAM_ESBUILD_BIN and TANGRAM_COMPONENT_NODE_MODULES"
        )
    runtime = preview / "ui-runtime"
    if runtime.is_symlink():
        raise LocalRuntimeError(".preview/ui-runtime is a symlink; refusing UI setup")
    modules = runtime / "node_modules"
    esbuild = modules / ".bin/esbuild"
    marker = runtime / ".tangram-component-catalog.json"
    wanted = json.dumps(_COMPONENT_CATALOG, sort_keys=True, separators=(",", ":"))
    try:
        current = marker.read_text(encoding="utf-8") if marker.is_file() else ""
        if current == wanted and modules.is_dir() and esbuild.exists():
            return esbuild.resolve(), modules.resolve()
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "package.json").write_text(
            json.dumps(
                {
                    "name": "tangram-local-component-runtime",
                    "private": True,
                    "dependencies": _COMPONENT_CATALOG,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        completed = subprocess.run(
            [npm, "install", "--no-audit", "--no-fund", "--prefix", str(runtime)],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, **environment},
            timeout=600,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[-3000:]
            raise LocalRuntimeError(f"installing UI runtime dependencies failed: {detail}")
        if not esbuild.exists():
            raise LocalRuntimeError("the managed UI runtime did not install esbuild")
        marker.write_text(wanted, encoding="utf-8")
        return esbuild.resolve(), modules.resolve()
    except subprocess.TimeoutExpired as error:
        raise LocalRuntimeError("installing UI runtime dependencies timed out") from error
    except OSError as error:
        raise LocalRuntimeError(f"could not prepare the UI runtime: {error}") from error


def _compile_component(
    component_dir: Path,
    entry_path: Path,
    esbuild: Path,
    node_modules: Path,
) -> UiBundle:
    with tempfile.TemporaryDirectory(prefix="tangram-ui-bundle-") as directory:
        stage = Path(directory)
        for source in component_dir.rglob("*"):
            if source.is_symlink():
                raise LocalRuntimeError(f"UI source contains a symlink: {source.name}")
            if not source.is_file():
                continue
            relative = source.relative_to(component_dir)
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        relative_entry = entry_path.relative_to(component_dir)
        entry = stage / relative_entry
        source_text = entry.read_text(encoding="utf-8")
        if "export default" in source_text:
            bootstrap = stage / "__tangram_bootstrap.tsx"
            import_path = "./" + relative_entry.as_posix()
            bootstrap.write_text(
                "import * as AppModule from "
                + json.dumps(import_path)
                + ";\nimport {createRoot} from 'react-dom/client';\n"
                + "import {createElement} from 'react';\n"
                + "const root=document.getElementById('root');\n"
                + "if(root&&AppModule.default) createRoot(root).render(createElement(AppModule.default));\n",
                encoding="utf-8",
            )
            entry = bootstrap
        output = stage / "bundle.js"
        environment = {**os.environ, "NODE_PATH": str(node_modules)}
        try:
            completed = subprocess.run(
                [
                    str(esbuild),
                    str(entry),
                    "--bundle",
                    "--format=iife",
                    "--platform=browser",
                    "--jsx=automatic",
                    "--minify",
                    f"--outfile={output}",
                ],
                cwd=stage,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LocalRuntimeError(f"UI compilation failed: {error}") from error
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[-4000:]
            raise LocalRuntimeError(f"esbuild failed: {detail}")
        css_path = stage / "bundle.css"
        javascript = output.read_bytes()
        stylesheet = css_path.read_bytes() if css_path.is_file() else None
        if len(javascript) > _MAX_BUNDLE:
            raise LocalRuntimeError("compiled UI JavaScript exceeds the local size limit")
        if stylesheet is not None and len(stylesheet) > _MAX_CSS:
            raise LocalRuntimeError("compiled UI CSS exceeds the local size limit")
        return UiBundle(javascript, stylesheet)


def _handler_factory(session: "LocalAppSession", bundle: UiBundle):
    sdk = _browser_sdk().encode("utf-8")
    html = _shell(bundle.stylesheet is not None).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", html, shell=True)
            elif path == "/sdk.js":
                self._send(200, "application/javascript; charset=utf-8", sdk)
            elif path == "/bundle.js":
                self._send(200, "application/javascript; charset=utf-8", bundle.javascript)
            elif path == "/bundle.css" and bundle.stylesheet is not None:
                self._send(200, "text/css; charset=utf-8", bundle.stylesheet)
            elif path == "/healthz":
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if urlsplit(self.path).path != "/action" or not _loopback_origin(
                self.headers.get("Origin")
            ):
                self._json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length < 0 or length > _MAX_ACTION_BODY:
                self._json(413, {"error": "action request is too large"})
                return
            try:
                body = json.loads(self.rfile.read(length))
                resource_type = body["resourceType"]
                action = body["action"]
                arguments = _flatten_action_arguments(body.get("args", {}))
                confirmed = body.get("confirmed") is True
                result = _invoke_ui_action(
                    session, resource_type, action, arguments, confirmed=confirmed
                )
                self._json(200, {"envelope": {"ok": True, "data": result}})
            except UiConfirmationRequired as error:
                self._json(
                    409,
                    {"code": "CONFIRMATION_REQUIRED", "error": str(error)},
                )
            except (KeyError, TypeError, ValueError) as error:
                self._json(400, {"error": f"invalid action request: {error}"})
            except Exception as error:
                self._json(400, {"error": str(error)[:2000]})

        def _send(
            self,
            status: int,
            content_type: str,
            payload: bytes,
            *,
            shell: bool = False,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if shell:
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; script-src 'self'; connect-src 'self'; "
                    "img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                    "base-uri 'none'; object-src 'none'; form-action 'none'",
                )
                self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, status: int, value: Any) -> None:
            self._send(
                status,
                "application/json; charset=utf-8",
                json.dumps(value, separators=(",", ":")).encode("utf-8"),
            )

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    return Handler


def _invoke_ui_action(
    session: "LocalAppSession",
    resource_type: str,
    action_name: str,
    arguments: Any,
    *,
    confirmed: bool,
) -> Any:
    matches = [
        action
        for action in session.app.graph.actions
        if action.resource_type == resource_type and action.name == action_name
    ]
    if len(matches) != 1:
        raise ValueError(f"app declares no unambiguous action {resource_type}.{action_name}")
    action = matches[0]
    gated = action.effect.value == "Irreversible" or action.requires_confirmation
    if gated and not confirmed:
        detail = (
            "is irreversible and cannot be undone"
            if action.effect.value == "Irreversible"
            else "requires confirmation before it runs"
        )
        raise UiConfirmationRequired(f"{resource_type}.{action_name} {detail}")
    policy = LocalDevelopmentPolicy(
        allow_mutations={action.id},
        preauthorized_confirmations={action.id} if confirmed else set(),
    )
    app = session.app.bind(
        backend=session.backend_url,
        policy=policy,
        timeout_seconds=session.request_timeout_seconds,
    )
    return asyncio.run(app.call(action.id, arguments))


def _flatten_action_arguments(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    result = {
        key: item
        for key, item in value.items()
        if key not in {"parameters", "requestBody"}
    }
    parameters = value.get("parameters")
    request_body = value.get("requestBody")
    if isinstance(parameters, Mapping):
        result.update(parameters)
    if isinstance(request_body, Mapping):
        result.update(request_body)
    return result


def _loopback_origin(origin: str | None) -> bool:
    if not origin:
        return True
    try:
        host = urlsplit(origin).hostname
        return host in {"127.0.0.1", "::1", "localhost"}
    except ValueError:
        return False


def _shell(has_css: bool) -> str:
    stylesheet = '<link rel="stylesheet" href="/bundle.css">' if has_css else ""
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        + stylesheet
        + "</head><body><div id=\"root\"></div>"
        '<script src="/sdk.js"></script><script src="/bundle.js"></script>'
        "</body></html>"
    )


def _browser_sdk() -> str:
    return r'''(function(){
"use strict";
var stateProvider=null;
function askConfirm(resourceType,action,detail){
  var ok=false;try{ok=window.confirm("Allow "+resourceType+"."+action+"? "+detail);}catch(e){}
  return Promise.resolve(ok);
}
window.tangram={
  getInputs:function(){return Promise.resolve({});},onInput:function(cb){if(cb)cb({});},
  getTheme:function(){return Promise.resolve(null);},onTheme:function(cb){if(cb)cb(null);},
  query:function(){return Promise.resolve({data:[],truncated:false,preview:true});},
  action:function(){return Promise.reject(new Error("binding actions are not available locally"));},
  performAction:function(ref,args){
    var body={resourceType:ref&&ref.resourceType,action:ref&&ref.action,args:args||{}};
    var frozen=JSON.stringify(body);
    function send(confirmed){
      var value=JSON.parse(frozen);if(confirmed)value.confirmed=true;
      return fetch("/action",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(value)})
        .then(function(res){return res.json().catch(function(){return {};}).then(function(j){return {ok:res.ok,status:res.status,json:j};});});
    }
    function unwrap(r){
      if(!r.ok)throw new Error((r.json&&r.json.error)||("action failed (HTTP "+r.status+")"));
      var env=r.json&&r.json.envelope;return env?env.data:r.json;
    }
    return send(false).then(function(r){
      if(!r.ok&&r.json&&r.json.code==="CONFIRMATION_REQUIRED"){
        return askConfirm(body.resourceType,body.action,r.json.error||"").then(function(granted){
          if(!granted)throw new Error("confirmation declined");return send(true).then(unwrap);
        });
      }
      return unwrap(r);
    });
  },
  emit:function(event,payload){window.dispatchEvent(new CustomEvent("tangram:output",{detail:{event:event,payload:payload}}));},
  openUrl:function(url){window.open(url,"_blank","noopener")},
  provideState:function(fn){stateProvider=typeof fn==="function"?fn:null;}
};
})();'''
