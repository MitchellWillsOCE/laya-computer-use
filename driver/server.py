"""Localhost API that Grok Bot calls between Laya click runs."""

from __future__ import annotations

import json
import queue
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from driver.engine import ROOT, Engine

HOST = "127.0.0.1"
PORT = 8765
TOKEN_PATH = ROOT / ".token"
MAX_BODY = 1_000_000


class DriverServer:
    def __init__(self) -> None:
        self.engine = Engine()
        self.jobs: queue.Queue = queue.Queue()
        self.ready = threading.Event()
        self.boot_error: str | None = None
        self.token = _load_token()
        self.worker = threading.Thread(target=self._worker, name="laya-driver", daemon=True)

    def serve(self) -> None:
        self.worker.start()
        print("Loading Laya and opening Chromium. The first run downloads the checkpoint.", flush=True)
        if not self.ready.wait(timeout=900):
            raise TimeoutError("Laya did not finish loading within 15 minutes.")
        if self.boot_error:
            raise RuntimeError(self.boot_error)
        handler = _handler_factory(self)
        httpd = ThreadingHTTPServer((HOST, PORT), handler)
        print(f"Ready at http://{HOST}:{PORT}", flush=True)
        print(f"Token file: {TOKEN_PATH}", flush=True)
        print("Leave this window open. Grok Bot drives it with the local prompt.", flush=True)
        try:
            httpd.serve_forever()
        finally:
            httpd.server_close()
            self.engine.close()

    def _worker(self) -> None:
        try:
            self.engine.start()
        except Exception as exc:
            self.boot_error = str(exc)
            self.ready.set()
            return
        self.ready.set()
        while True:
            job = self.jobs.get()
            if job["cancelled"].is_set():
                continue
            try:
                result = self._dispatch(job)
            except Exception as exc:
                message = str(exc)
                result = self.engine.mark_error(message)
                print(f"error: {exc}", flush=True)
            job["result"].put(result)

    def _dispatch(self, job: dict) -> dict:
        kind = job["kind"]
        if kind == "status":
            return self.engine.status_packet()
        if kind == "elements":
            return self.engine.elements_packet(job.get("query"))
        if kind == "screenshot":
            return {"path": self.engine.ensure_screenshot()}
        if kind == "error":
            return self.engine.mark_error(job.get("message") or "unknown error")
        if kind == "task":
            return self.engine.task(
                job.get("goal") or "",
                job.get("url"),
                bool(job.get("restart")),
                job.get("max_steps"),
                job.get("observe_cap"),
                job.get("element_limit"),
            )
        if kind == "reply":
            return self.engine.reply(
                job.get("text"),
                job.get("action"),
                job.get("element_id"),
                bool(job.get("submit")),
                job,
            )
        if kind == "stop":
            self.engine.pending = None
            self.engine.goal = ""
            self.engine.status = "idle"
            self.engine.last_error = None
            return self.engine.status_packet()
        return self.engine.mark_error(f"unknown job {kind}")

    def submit(self, kind: str, payload: dict, timeout: float) -> dict:
        if not self.ready.is_set():
            return self.engine.fallback_error_packet("driver is still starting")
        if self.boot_error:
            return self.engine.fallback_error_packet(self.boot_error)
        result: queue.Queue = queue.Queue(maxsize=1)
        cancelled = threading.Event()
        self.jobs.put(
            {
                **payload,
                "kind": kind,
                "result": result,
                "cancelled": cancelled,
            }
        )
        try:
            return result.get(timeout=timeout)
        except queue.Empty:
            cancelled.set()
            if kind in {"task", "reply"}:
                self.engine.request_interrupt()
            return self.engine.fallback_error_packet(
                "timed out waiting for the browser step"
            )


def _load_token() -> str:
    if TOKEN_PATH.exists():
        return TOKEN_PATH.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(24)
    TOKEN_PATH.write_text(token, encoding="utf-8")
    return token


def _handler_factory(server: DriverServer):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            print(f"http {self.address_string()} {fmt % args}", flush=True)

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/health":
                body = {"ok": server.boot_error is None and server.ready.is_set()}
                body.update(server.engine.snapshot() if server.ready.is_set() else {"status": "starting"})
                if server.boot_error:
                    body["error"] = server.boot_error
                self._send(200, body)
                return
            if parsed.path == "/status":
                if not self._authorized():
                    return
                self._send(200, server.submit("status", {}, timeout=30))
                return
            if parsed.path == "/elements":
                if not self._authorized():
                    return
                query = parse_qs(parsed.query).get("query", [None])[0]
                self._send(
                    200,
                    server.submit("elements", {"query": query}, timeout=30),
                )
                return
            if parsed.path == "/screenshot":
                if not self._authorized():
                    return
                result = server.submit("screenshot", {}, timeout=30)
                raw_path = result.get("path")
                path = Path(raw_path) if raw_path else None
                if not path or not path.is_file():
                    self._send(
                        404,
                        server.submit(
                            "error",
                            {"message": "no screenshot is available"},
                            timeout=30,
                        ),
                    )
                    return
                self._send_bytes(200, path.read_bytes(), "image/png")
                return
            self._send(
                404,
                server.engine.fallback_error_packet(
                    "not found", mutate=False
                ),
            )

        def do_POST(self) -> None:
            if not self._authorized():
                return
            path = urlsplit(self.path).path
            payload = self._read_json()
            if payload is None:
                return
            if path == "/task":
                server.engine.request_interrupt()
                self._send(200, server.submit("task", payload, timeout=600))
                return
            if path == "/reply":
                self._send(200, server.submit("reply", payload, timeout=600))
                return
            if path == "/stop":
                self._send(200, server.submit("stop", {}, timeout=30))
                return
            self._send(
                404,
                server.engine.fallback_error_packet(
                    "not found", mutate=False
                ),
            )

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            presented = header.removeprefix("Bearer ").strip()
            if header.startswith("Bearer ") and secrets.compare_digest(presented, server.token):
                return True
            self._send(
                401,
                server.engine.fallback_error_packet(
                    "unauthorized", mutate=False
                ),
            )
            return False

        def _read_json(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                self._send(
                    400,
                    server.submit(
                        "error",
                        {"message": "invalid Content-Length"},
                        timeout=30,
                    ),
                )
                return None
            if length > MAX_BODY:
                self._send(
                    413,
                    server.submit(
                        "error", {"message": "body too large"}, timeout=30
                    ),
                )
                return None
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send(
                    400,
                    server.submit(
                        "error",
                        {"message": "body must be JSON"},
                        timeout=30,
                    ),
                )
                return None
            if not isinstance(data, dict):
                self._send(
                    400,
                    server.submit(
                        "error",
                        {"message": "body must be a JSON object"},
                        timeout=30,
                    ),
                )
                return None
            return data

        def _send(self, code: int, payload: dict) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _send_bytes(
            self, code: int, payload: bytes, content_type: str
        ) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler
