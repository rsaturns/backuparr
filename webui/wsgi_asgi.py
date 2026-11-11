"""Flask (a WSGI app) under Hypercorn's ASGI server (#177).

Hypercorn's own WSGI wrapper fails any response with an empty body (a
browser's 304 Not Modified becomes a 500; pgjones/hypercorn#331), and
asgiref's runs every request on one shared thread, so one download would
hold up the whole web UI. This adapter follows PEP 3333 instead:

- Requests run in a pool of threads; a large upload waits in a temporary
  file, not in memory, and anything over the size limit gets 413.
- Headers are sent with the first non-empty chunk of the body, or when the
  body ends empty, so an app can still swap them for an error until then.
- Each chunk is sent before the next is read (backpressure), so a large
  download never sits in memory, and a download stops when the browser
  goes away.
- The response's close() always runs, which Flask's teardown needs."""
import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from tempfile import SpooledTemporaryFile

SPOOL_IN_MEMORY = 1024 * 1024


class ClientGone(Exception):
    pass


def build_environ(scope, body):
    server = scope.get("server") or ("localhost", 80)
    path = scope["path"]
    root = scope.get("root_path", "")
    if root and path.startswith(root):
        path = path[len(root):] or "/"
    environ = {
        "REQUEST_METHOD": scope["method"],
        "SCRIPT_NAME": root.encode("utf-8").decode("latin-1"),
        "PATH_INFO": path.encode("utf-8").decode("latin-1"),
        "QUERY_STRING": scope["query_string"].decode("latin-1"),
        "SERVER_NAME": str(server[0]),
        "SERVER_PORT": str(server[1]),
        "SERVER_PROTOCOL": f"HTTP/{scope['http_version']}",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": scope.get("scheme", "http"),
        "wsgi.input": body,
        "wsgi.errors": sys.stderr,
        "wsgi.multithread": True,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    if scope.get("client"):
        environ["REMOTE_ADDR"] = scope["client"][0]
    for raw_name, raw_value in scope.get("headers", []):
        name = raw_name.decode("latin-1").lower()
        key = {"content-length": "CONTENT_LENGTH", "content-type": "CONTENT_TYPE"}.get(
            name, "HTTP_" + name.upper().replace("-", "_"))
        value = raw_value.decode("latin-1")
        if key in environ:
            value = environ[key] + ("; " if key == "HTTP_COOKIE" else ",") + value
        environ[key] = value
    return environ


async def _plain(send, status, text):
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(text)).encode())]})
    await send({"type": "http.response.body", "body": text})


class WsgiToAsgi:
    def __init__(self, app, max_body, threads=16):
        self.app = app
        self.max_body = max_body
        self.executor = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="web")

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        with SpooledTemporaryFile(max_size=SPOOL_IN_MEMORY) as body:
            size = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > self.max_body:
                    await _plain(send, 413, b"Request too large")
                    return
                body.write(chunk)
                if not message.get("more_body"):
                    break
            body.seek(0)
            gone = asyncio.Event()

            async def watch():
                while (await receive())["type"] != "http.disconnect":
                    pass
                gone.set()

            watcher = asyncio.create_task(watch())
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(self.executor, self._run, build_environ(scope, body), send, loop, gone)
            finally:
                watcher.cancel()

    def _run(self, environ, send, loop, gone):
        """Runs the app in a pool thread."""
        state = {"start": None, "sent": False}

        def blocking_send(message):
            if gone.is_set():
                raise ClientGone
            try:
                asyncio.run_coroutine_threadsafe(send(message), loop).result()
            except Exception as exc:  # the connection closed under us
                raise ClientGone from exc

        def send_headers():
            if not state["sent"]:
                if state["start"] is None:
                    raise RuntimeError("the WSGI app didn't call start_response")
                blocking_send(state["start"])
                state["sent"] = True

        def write(data):
            if data:
                send_headers()
                blocking_send({"type": "http.response.body", "body": bytes(data), "more_body": True})

        def start_response(status, headers, exc_info=None):
            if exc_info:
                try:
                    if state["sent"]:
                        raise exc_info[1].with_traceback(exc_info[2])
                finally:
                    exc_info = None
            elif state["start"] is not None:
                raise RuntimeError("start_response called twice without exc_info")
            state["start"] = {"type": "http.response.start", "status": int(status.split(" ", 1)[0]),
                              "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers]}
            return write

        result = self.app(environ, start_response)
        try:
            for chunk in result:
                write(chunk)
            send_headers()
            blocking_send({"type": "http.response.body", "body": b"", "more_body": False})
        except ClientGone:
            pass
        finally:
            close = getattr(result, "close", None)
            if close is not None:
                close()
