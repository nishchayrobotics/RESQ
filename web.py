"""Serve the local application and handle browser requests."""

import hmac
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from Frontend import views
from application import ActionError, DispatchApp, build_review

MAX_BODY_BYTES = 16 * 1024
MAX_FIELDS = 32
DEFAULT_PORT = 8765

SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; "
                                "frame-ancestors 'none'; base-uri 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}

PAGES = ("desk", "resources", "archive", "shifts")
GET_ROUTES = {"/": "desk", "/desk": "desk", "/resources": "resources", "/archive": "archive", "/shifts": "shifts"}


POST_ROUTES = {
    "/call/receive": ("_post_receive", "desk"),
    "/incident/report": ("_post_report", "desk"),
    "/dispatch/confirm": ("_post_confirm", "desk"),
    "/time/advance": ("_post_advance", "desk"),
    "/road/toggle": ("_post_road", "resources"),
    "/hospital/discharge": ("_post_discharge", "resources"),
    "/shift/save": ("_post_save", "shifts"),
    "/shift/restore": ("_post_restore", "shifts"),
    "/shift/new": ("_post_new", "shifts"),
}


class ResqServer(ThreadingHTTPServer):
    """A threaded local server that knows its RESQ application."""

    daemon_threads = True

    def __init__(self, app: DispatchApp, static_dir: Path, port: int = DEFAULT_PORT, quiet: bool = False):
        super().__init__(("127.0.0.1", port), ResqHandler)
        self.app = app
        self.stylesheet = Path(static_dir) / "style.css"
        self.quiet = quiet
        actual_port = self.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"}
        self.allowed_origins = {f"http://{host}" for host in self.allowed_hosts}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class ResqHandler(BaseHTTPRequestHandler):
    server: ResqServer
    server_version = "RESQ/1.0"
    sys_version = ""


    def log_message(self, format, *args) -> None:
        if not self.server.quiet:
            super().log_message(format, *args)

    def _send(self, status: int, body: bytes, content_type: str, extra_headers=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in {**SECURITY_HEADERS, **(extra_headers or {})}.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_page(self, status: int, page) -> None:
        self._send(status, str(page).encode("utf-8"), "text/html; charset=utf-8")

    def _send_error_page(self, status: int, title: str, message: str) -> None:
        self.close_connection = True
        self._send_page(status, views.error_page(title, message))

    def _redirect(self, page: str, message: str) -> None:
        key = self.server.app.add_notice(message)
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", f"/{page}?notice={key}")
        self.send_header("Content-Length", "0")
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()

    def _host_allowed(self) -> bool:
        if self.headers.get("Host", "") in self.server.allowed_hosts:
            return True
        self._send_error_page(HTTPStatus.BAD_REQUEST, "Unexpected address",
                              f"RESQ only answers at {self.server.url}.")
        return False


    def _context(self, notice=None) -> views.ViewContext:
        snapshot = self.server.app.snapshot()
        return views.ViewContext(snapshot.state, snapshot.deck, snapshot.revision, self.server.app.form_token, notice)

    def _render(self, page: str, status: int = 200, notice=None, review: bool = False,
                form_errors=None, form_values=None, choice=None) -> None:
        app = self.server.app
        ctx = self._context(notice)
        if page == "desk":
            choice = choice or {}
            review_data = build_review(ctx.state, choice.get("ambulance"), choice.get("hospital"))
            fields = None
            if review and review_data.plan is not None:
                fields = app.plan_form_fields(review_data.plan, ctx.revision)
            body = views.desk_page(ctx, review_data, review, fields, form_errors, form_values)
        elif page == "resources":
            body = views.resources_page(ctx)
        elif page == "archive":
            body = views.archive_page(ctx)
        else:
            body = views.shifts_page(ctx, app.checkpoints(), saving_enabled=app.saves_dir is not None)
        self._send_page(status, body)

    def _guarded(self, handler) -> None:
        """Run a handler; an unexpected bug becomes a friendly 500 page, not a dropped connection."""
        try:
            handler()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.log_error("Unexpected error while handling %s:\n%s", self.path, traceback.format_exc())
            self._send_error_page(HTTPStatus.INTERNAL_SERVER_ERROR, "Something went wrong",
                                  "RESQ hit an unexpected problem. Your shift was not changed by this request.")

    def do_GET(self) -> None:
        self._guarded(self._handle_get)

    def do_POST(self) -> None:
        self._guarded(self._handle_post)

    def _handle_get(self) -> None:
        if not self._host_allowed():
            return
        url = urlsplit(self.path)
        if url.path == "/static/style.css":
            try:
                css = self.server.stylesheet.read_bytes()
            except OSError:
                self._send_error_page(HTTPStatus.NOT_FOUND, "Not found", "The stylesheet is missing.")
                return
            self._send(HTTPStatus.OK, css, "text/css; charset=utf-8", {"Cache-Control": "no-cache"})
            return
        if url.path == "/favicon.ico":
            self._send(HTTPStatus.NO_CONTENT, b"", "text/plain")
            return
        page = GET_ROUTES.get(url.path)
        if page is None:
            self._send_error_page(HTTPStatus.NOT_FOUND, "Page not found", "That address is not part of RESQ.")
            return
        query = dict(parse_qsl(url.query, max_num_fields=8))
        notice = self.server.app.notice(query.get("notice"))
        choice = {key: query[key] for key in ("ambulance", "hospital") if query.get(key)}
        self._render(page, notice=notice, review=query.get("review") == "1", choice=choice)


    def _read_form(self) -> dict[str, str] | None:
        """Read a small url-encoded body, or send an error page and return None."""
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            self._send_error_page(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Unsupported form", "RESQ expects a normal web form.")
            return None
        length_text = self.headers.get("Content-Length")
        if length_text is None or not length_text.isdigit():
            self._send_error_page(HTTPStatus.LENGTH_REQUIRED, "Missing length", "The form did not say how long it was.")
            return None
        length = int(length_text)
        if length > MAX_BODY_BYTES:
            self._send_error_page(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Form too large",
                                  "That form was far larger than any RESQ form, so it was ignored.")
            return None
        try:
            text = self.rfile.read(length).decode("utf-8")
            pairs = parse_qsl(text, keep_blank_values=True, max_num_fields=MAX_FIELDS)
        except (UnicodeDecodeError, ValueError):
            self._send_error_page(HTTPStatus.BAD_REQUEST, "Unreadable form", "The form could not be read.")
            return None
        form: dict[str, str] = {}
        for name, value in pairs:
            form.setdefault(name, value)
        return form

    def _handle_post(self) -> None:
        if not self._host_allowed():
            return
        route = POST_ROUTES.get(urlsplit(self.path).path)
        if route is None:
            self._send_error_page(HTTPStatus.NOT_FOUND, "Page not found", "That action is not part of RESQ.")
            return
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.server.allowed_origins:
            self._send_error_page(HTTPStatus.FORBIDDEN, "Blocked", "Forms may only be sent from RESQ itself.")
            return
        form = self._read_form()
        if form is None:
            return
        if not hmac.compare_digest(form.get("token", "").encode(), self.server.app.form_token.encode()):
            self._send_error_page(HTTPStatus.FORBIDDEN, "Form expired",
                                  "This page came from an earlier run of RESQ (or another site). "
                                  "Open the dispatch desk again and retry.")
            return
        method_name, page = route
        try:
            message = getattr(self, method_name)(form)
        except ActionError as error:
            self._render(page, status=error.status, notice=("error", error.message),
                         form_errors=error.field_errors, form_values=error.values)
            return
        self._redirect(page, message)


    def _post_receive(self, form):
        return self.server.app.receive_call(form.get("revision"))

    def _post_report(self, form):
        return self.server.app.report_incident(form)

    def _post_confirm(self, form):
        return self.server.app.confirm_dispatch(form)

    def _post_advance(self, form):
        return self.server.app.advance(form.get("revision"), form.get("minutes", ""))

    def _post_road(self, form):
        return self.server.app.set_road(form.get("revision"), form.get("road", ""), form.get("action", ""))

    def _post_discharge(self, form):
        return self.server.app.discharge(form.get("revision"), form.get("hospital", ""), form.get("count", ""))

    def _post_save(self, form):
        return self.server.app.save_shift(form.get("revision"), form.get("label", ""))

    def _post_restore(self, form):
        return self.server.app.restore_shift(form.get("revision"), form.get("name", ""), form.get("confirm", ""))

    def _post_new(self, form):
        return self.server.app.new_shift(form.get("revision"), form.get("confirm", ""))


def run_server(app: DispatchApp, static_dir: Path, port: int = DEFAULT_PORT) -> int:
    """Start serving until Ctrl+C."""
    try:
        server = ResqServer(app, static_dir, port)
    except OSError as error:
        print(f"RESQ could not start on port {port}: {error.strerror or error}.")
        print(f"Is RESQ already running in another terminal? Close it, or try: --port {port + 1}")
        return 1
    print("RESQ is running.")
    print(f"Open this address in your browser:  {server.url}")
    print("Keep this terminal open while you use RESQ. Press Ctrl+C to stop.")
    print("Stopping discards anything you have not saved under Saved shifts.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nRESQ stopped. Unsaved shift progress was discarded; saved checkpoints remain in the saves folder.")
    finally:
        server.server_close()
    return 0
