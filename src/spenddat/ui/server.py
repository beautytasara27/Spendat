"""Localhost-only HTTP server for the web UI (stdlib only, no framework).

Security:
* binds to 127.0.0.1 by default and refuses non-loopback hosts unless explicitly allowed;
* checks the ``Host`` header against the bound address (DNS-rebinding protection);
* state-changing requests must carry ``X-Spenddat: 1`` and a same-origin ``Origin``; a
  custom header cannot be sent cross-site without a CORS preflight, which is never granted.
"""

from __future__ import annotations

import json
import mimetypes
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlparse

from spenddat.models import RuntimeMode
from spenddat.ui.service import UiService

STATIC = resources.files("spenddat.ui") / "static"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def make_handler(service: UiService, allowed_hosts: set[str]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "spenddat-ui"

        # ------------------------------------------------------------------ plumbing

        def log_message(self, fmt: str, *args: Any) -> None:  # keep the console clean
            pass

        def _host_ok(self) -> bool:
            return (self.headers.get("Host") or "").lower() in allowed_hosts

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com;"
                " font-src 'self' https://fonts.gstatic.com data:; img-src 'self' data:;"
                " script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            self._send(status, json.dumps(payload, default=str).encode(), "application/json")

        def _body(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 64_000:
                raise ValueError("request too large")
            return json.loads(self.rfile.read(n) or b"{}")

        # ------------------------------------------------------------------ routes

        def do_GET(self) -> None:
            if not self._host_ok():
                return self._json(403, {"error": "forbidden host"})
            url = urlparse(self.path)
            q = parse_qs(url.query)
            path = url.path
            try:
                if path == "/api/summary":
                    return self._json(200, service.summary())
                if path == "/api/transactions":
                    m = q.get("mode", ["current"])[0]
                    mode = (
                        None
                        if m == "all"
                        else service.orch.mode
                        if m == "current"
                        else RuntimeMode(m)
                    )
                    return self._json(
                        200, service.transactions(mode, int(q.get("limit", ["300"])[0]))
                    )
                if path.startswith("/api/transactions/"):
                    d = service.transaction_detail(path.rsplit("/", 1)[1])
                    return self._json(200, d) if d else self._json(404, {"error": "not found"})
                if path == "/api/approvals":
                    return self._json(200, service.approvals())
                if path == "/api/stores":
                    raw = q.get("check", ["0"])[0]
                    check: bool | str = (
                        True if raw == "1" else (raw if raw not in ("", "0") else False)
                    )
                    return self._json(200, service.stores(check=check))
                return self._static(path)
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def do_POST(self) -> None:
            if not self._host_ok():
                return self._json(403, {"error": "forbidden host"})
            origin = self.headers.get("Origin")
            if self.headers.get("X-Spenddat") != "1" or (
                origin and urlparse(origin).netloc.lower() not in allowed_hosts
            ):
                return self._json(403, {"error": "cross-site request refused"})
            path = urlparse(self.path).path
            try:
                body = self._body()
                if path == "/api/chat":
                    msg = str(body.get("message", ""))[:2000]
                    return self._json(200, service.chat(body.get("conversation_id"), msg))
                if path.startswith("/api/approvals/"):
                    apr_id = path.rsplit("/", 1)[1]
                    action = str(body.get("action", ""))
                    if action not in ("approve", "deny"):
                        return self._json(400, {"error": "action must be approve or deny"})
                    blocks = service.decide(
                        apr_id,
                        action,
                        max_total_minor=_int_or_none(body.get("max_total_minor")),
                        quantity=_int_or_none(body.get("quantity")),
                    )
                    return self._json(200, {"blocks": blocks})
                if path == "/api/stores":
                    return self._json(200, service.change_store("add", None, body))
                m = re.fullmatch(r"/api/stores/([a-z][a-z0-9_]{1,39})(/remove)?", path)
                if m:
                    action = "remove" if m.group(2) else "update"
                    return self._json(200, service.change_store(action, m.group(1), body))
                return self._json(404, {"error": "not found"})
            except (ValueError, json.JSONDecodeError) as e:
                return self._json(400, {"error": str(e)})
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def _static(self, path: str) -> None:
            name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
            if "/" in name or name.startswith(".") or not name:
                return self._json(404, {"error": "not found"})
            f = STATIC / name
            if not f.is_file():
                return self._json(404, {"error": "not found"})
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            return self._send(200, f.read_bytes(), ctype)

    return Handler


def _int_or_none(v: Any) -> int | None:
    if v in (None, ""):
        return None
    i = int(v)
    if i <= 0:
        raise ValueError("values must be positive")
    return i


def serve(
    service: UiService,
    host: str = "127.0.0.1",
    port: int = 8420,
    *,
    open_browser: bool = True,
    allow_remote: bool = False,
) -> None:
    if host not in LOOPBACK and not allow_remote:
        raise ValueError("refusing to bind a non-loopback address without --allow-remote")
    names = LOOPBACK | {host}
    allowed = {f"{h}:{port}" for h in names} | {f"[::1]:{port}"}
    httpd = ThreadingHTTPServer((host, port), make_handler(service, allowed))
    url = f"http://{'127.0.0.1' if host in LOOPBACK else host}:{port}/"
    print(
        f"Spenddat UI running at {url}  [{service.orch.mode.value.upper()} mode, "
        f"intent: {service.llm_label}]  (Ctrl+C to stop)"
    )
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
