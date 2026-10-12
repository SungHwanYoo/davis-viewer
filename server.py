"""Local DAVIS comparison site. Only predefined image paths are exposed."""

from __future__ import annotations

import argparse
import json
import threading
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from core import Catalog, EventRenderer, LikeStore


WEB_DIR = Path(__file__).with_name("web")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}


def create_server(host: str, port: int, recordings: Path, state_dir: Path) -> ThreadingHTTPServer:
    hidden_path = Path(state_dir) / "hidden_sessions.json"
    hidden_ids = json.loads(hidden_path.read_text(encoding="utf-8")) if hidden_path.is_file() else []
    if not isinstance(hidden_ids, list) or any(not isinstance(value, str) for value in hidden_ids):
        raise ValueError("hidden_sessions.json must contain a list of session IDs")
    catalog = Catalog(recordings, excluded_ids=hidden_ids)
    likes = LikeStore(Path(state_dir) / "likes.sqlite3")
    renderers: dict[str, EventRenderer] = {}
    renderers_lock = threading.Lock()
    render_limit = threading.BoundedSemaphore(2)
    bin_render_lock = threading.Lock()

    def renderer_for(session) -> EventRenderer:
        with renderers_lock:
            renderer = renderers.get(session.id)
            if renderer is None:
                renderer = EventRenderer(session)
                renderers[session.id] = renderer
            return renderer

    class Handler(BaseHTTPRequestHandler):
        server_version = "DAVISViewer/1.0"

        def _reply(self, status: int, body: bytes, content_type: str, *, cookie: str | None = None,
                   auth_cookie: str | None = None,
                   cache: str = "no-store") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; "
                             "script-src 'self'; style-src 'self'; connect-src 'self'")
            if cookie:
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                self.send_header("Set-Cookie", f"davis_visitor={cookie}; Path=/; Max-Age=31536000; "
                                 f"HttpOnly; SameSite=Lax{secure}")
            if auth_cookie is not None:
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                max_age = 30 * 86400 if auth_cookie else 0
                self.send_header("Set-Cookie", f"davis_auth={auth_cookie}; Path=/; Max-Age={max_age}; "
                                 f"HttpOnly; SameSite=Lax{secure}")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, value, *, cookie: str | None = None,
                  auth_cookie: str | None = None) -> None:
            self._reply(status, json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(),
                        "application/json; charset=utf-8", cookie=cookie, auth_cookie=auth_cookie)

        def _visitor(self) -> tuple[str, str | None]:
            cookies = SimpleCookie()
            try:
                cookies.load(self.headers.get("Cookie", ""))
                value = cookies["davis_visitor"].value
                return str(uuid.UUID(value)), None
            except (KeyError, ValueError):
                value = str(uuid.uuid4())
                return value, value

        def _auth_token(self) -> str | None:
            cookies = SimpleCookie()
            try:
                cookies.load(self.headers.get("Cookie", ""))
                token = cookies["davis_auth"].value
                return token if len(token) <= 128 else None
            except (KeyError, ValueError):
                return None

        def _account(self) -> dict | None:
            return likes.account_for_token(self._auth_token())

        def _post_body(self) -> dict | None:
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                self._json(403, {"error": "Origin not allowed"})
                return None
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self._json(415, {"error": "JSON required"})
                return None
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 1024:
                    raise ValueError()
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict):
                    raise ValueError()
                return value
            except (ValueError, json.JSONDecodeError):
                self._json(400, {"error": "Invalid JSON request"})
                return None

        def _session(self, name: str):
            try:
                return catalog.get(name)
            except KeyError:
                return None

        def do_GET(self):
            parsed = urlsplit(self.path)
            parts = parsed.path.strip("/").split("/")
            if parsed.path in STATIC_FILES:
                filename, content_type = STATIC_FILES[parsed.path]
                file = WEB_DIR / filename
                if file.is_file():
                    self._reply(200, file.read_bytes(), content_type)
                else:
                    self._json(404, {"error": "Not found"})
                return
            if parsed.path == "/health":
                self._json(200, {"status": "ok", "sessions": len(catalog.list_sessions())})
                return
            if parsed.path == "/api/sessions":
                self._json(200, catalog.list_sessions())
                return
            if parsed.path == "/api/auth/me":
                _, new_cookie = self._visitor()
                account = self._account()
                self._json(200, {"user": {"username": account["username"]} if account else None},
                           cookie=new_cookie)
                return
            if len(parts) == 3 and parts[:2] == ["api", "session"]:
                session = self._session(parts[2])
                if session is None:
                    self._json(404, {"error": "Session not found"})
                    return
                visitor, new_cookie = self._visitor()
                account = self._account()
                if account:
                    likes.reconcile_visitor(account["voter_id"], visitor)
                counts = likes.counts(session.id, account["voter_id"] if account else visitor)
                self._json(200, {
                    "session": session.public(),
                    "rows": [row.public() for row in session.rows],
                    "likes": {str(index): {"count": count, "mine": mine}
                              for index, (count, mine) in counts.items()},
                    "user": {"username": account["username"]} if account else None,
                }, cookie=new_cookie)
                return
            if len(parts) == 4 and parts[:2] == ["api", "likes"]:
                session = self._session(parts[2])
                try:
                    if session is None:
                        raise KeyError()
                    index = int(parts[3])
                    session.row(index)
                except (KeyError, ValueError):
                    self._json(404, {"error": "Frame not found"})
                    return
                self._json(200, likes.likers(session.id, index))
                return
            if len(parts) == 5 and parts[:2] == ["api", "image"]:
                session = self._session(parts[2])
                try:
                    path = session.image_path(int(parts[4]), parts[3]) if session else None
                except (KeyError, ValueError):
                    path = None
                if path is None:
                    self._json(404, {"error": "Image not found"})
                    return
                self._reply(200, path.read_bytes(), "image/png", cache="public, max-age=86400")
                return
            if (len(parts) == 4 and parts[:2] == ["api", "bins"]) or (
                len(parts) == 5 and parts[:2] == ["api", "bin"]
            ):
                session = self._session(parts[2])
                try:
                    index = int(parts[3])
                    if session is None:
                        raise ValueError()
                    session.row(index)
                    bin_index = int(parts[4]) if len(parts) == 5 else None
                    if bin_index is not None and not 0 <= bin_index < 30:
                        raise ValueError()
                except (KeyError, ValueError):
                    self._json(400, {"error": "Invalid session, frame, or bin"})
                    return
                try:
                    if not bin_render_lock.acquire(timeout=20):
                        self._json(503, {"error": "Event renderer busy; retry shortly"})
                        return
                    try:
                        batch = renderer_for(session).render_30_bins(index)
                    finally:
                        bin_render_lock.release()
                except Exception as exc:
                    self.log_error("30-bin render failed: %s", exc)
                    self._json(500, {"error": "Event bins unavailable"})
                    return
                if bin_index is None:
                    self._json(200, batch.public())
                else:
                    self._reply(200, batch.bins[bin_index].png, "image/png")
                return
            if len(parts) == 4 and parts[:2] == ["api", "event"]:
                session = self._session(parts[2])
                try:
                    index = int(parts[3])
                    ms = int(parse_qs(parsed.query).get("ms", ["33"])[0])
                    if not 1 <= ms <= 3000 or session is None:
                        raise ValueError()
                    session.row(index)
                except (KeyError, ValueError):
                    self._json(400, {"error": "Invalid session, frame, or interval"})
                    return
                try:
                    renderer = renderer_for(session)
                    if not render_limit.acquire(timeout=10):
                        self._json(503, {"error": "Event renderer busy; retry shortly"})
                        return
                    try:
                        png = renderer.render_png(index, ms)
                    finally:
                        render_limit.release()
                except Exception as exc:
                    self.log_error("Event render failed: %s", exc)
                    self._json(500, {"error": "Event image unavailable"})
                    return
                self._reply(200, png, "image/png", cache="public, max-age=86400")
                return
            self._json(404, {"error": "Not found"})

        def do_POST(self):
            parsed = urlsplit(self.path)
            parts = parsed.path.strip("/").split("/")
            if parsed.path not in ("/api/auth/login", "/api/auth/logout") and (
                len(parts) != 4 or parts[:2] != ["api", "like"]
            ):
                self._json(404, {"error": "Not found"})
                return
            body = self._post_body()
            if body is None:
                return
            if parsed.path == "/api/auth/login":
                visitor, new_cookie = self._visitor()
                result = likes.login(body.get("username"), body.get("password"), visitor)
                if result is None:
                    self._json(401, {"error": "아이디 또는 비밀번호가 맞지 않습니다."}, cookie=new_cookie)
                    return
                token, account = result
                self._json(200, {"user": {"username": account["username"]}},
                           cookie=new_cookie, auth_cookie=token)
                return
            if parsed.path == "/api/auth/logout":
                likes.logout(self._auth_token())
                self._json(200, {"user": None}, auth_cookie="")
                return
            account = self._account()
            visitor, new_cookie = self._visitor()
            if account:
                likes.reconcile_visitor(account["voter_id"], visitor)
            try:
                session = self._session(parts[2])
                index = int(parts[3])
                if session is None:
                    raise KeyError()
                session.row(index)
            except (ValueError, KeyError):
                self._json(400, {"error": "Invalid like request"})
                return
            try:
                liked, count = likes.toggle(session.id, index,
                                            account["voter_id"] if account else visitor)
            except Exception as exc:
                self.log_error("Like save failed: %s", exc)
                self._json(500, {"error": "Could not save like"})
                return
            self._json(200, {"liked": liked, "count": count}, cookie=new_cookie)

        def log_message(self, format, *args):
            print(f"[{self.log_date_time_string()}] {self.address_string()} {format % args}", flush=True)

    class Server(ThreadingHTTPServer):
        daemon_threads = True

    return Server((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description="Local DAVIS recording viewer")
    parser.add_argument("--recordings", type=Path, default=Path.home() / "davis_recordings")
    parser.add_argument("--state-dir", type=Path, default=Path(__file__).with_name("data"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = create_server(args.host, args.port, args.recordings, args.state_dir)
    print(f"DAVIS viewer: http://{args.host}:{server.server_port} ", flush=True)
    print(f"Recordings: {args.recordings}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
