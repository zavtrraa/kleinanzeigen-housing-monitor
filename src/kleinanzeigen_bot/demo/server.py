import asyncio
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from filelock import FileLock

from .session import DemoSession

ASSETS = Path(__file__).with_name("assets")
ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/assets/style.css": ("style.css", "text/css; charset=utf-8"),
    "/assets/apartment.svg": ("apartment.svg", "image/svg+xml"),
}


def create_server(session, loop, *, port=8765):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, content, content_type="application/json; charset=utf-8"):
            body = content if isinstance(content, bytes) else json.dumps(content).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'"
            )
            self.end_headers()
            self.wfile.write(body)

        def local_request(self):
            hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            origin = self.headers.get("Origin")
            return self.headers.get("Host") in hosts and (
                origin is None or origin in {"http://" + host for host in hosts}
            )

        def do_GET(self):
            if not self.local_request():
                self.respond(403, {"error": "Local requests only"})
                return
            path = urlsplit(self.path).path
            if path == "/api/state":

                async def snapshot():
                    return session.snapshot()

                result = asyncio.run_coroutine_threadsafe(snapshot(), loop).result(timeout=30)
                self.respond(200, result)
            elif path in ROUTES:
                filename, content_type = ROUTES[path]
                self.respond(200, (ASSETS / filename).read_bytes(), content_type)
            else:
                self.respond(404, {"error": "Not found"})

        def do_POST(self):
            if not self.local_request():
                self.respond(403, {"error": "Local requests only"})
                return
            if self.path != "/api/command":
                self.respond(404, {"error": "Not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.respond(400, {"error": "Invalid request length"})
                return
            if not 0 < length <= 4096:
                self.respond(413, {"error": "Request must be 1-4096 bytes"})
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self.respond(415, {"error": "Use application/json"})
                return
            try:
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or not isinstance(body.get("command"), str):
                    raise ValueError("A command string is required")
                future = asyncio.run_coroutine_threadsafe(
                    session.command(body["command"], body.get("message_id")), loop
                )
                result = future.result(timeout=30)
            except (ValueError, UnicodeError):
                self.respond(400, {"error": "Invalid command request"})
            except TimeoutError:
                future.cancel()
                self.respond(503, {"error": "Command timed out"})
            except Exception:
                self.respond(500, {"error": "Demo command failed; inspect the local process"})
            else:
                self.respond(200, result)

    return HTTPServer(("127.0.0.1", port), Handler)


async def serve_demo(directory: Path, port=8765):
    directory.mkdir(parents=True, exist_ok=True)
    with FileLock(str(directory / "demo.lock")).acquire(timeout=0):
        session = DemoSession(directory)
        server = None
        try:
            await session.start()
            server = create_server(session, asyncio.get_running_loop(), port=port)
            print(f"Local demo: http://127.0.0.1:{server.server_port}", flush=True)
            print("Fictional data. No Telegram credentials or seller messages.", flush=True)
            await asyncio.to_thread(server.serve_forever, poll_interval=0.2)
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
            session.close()
