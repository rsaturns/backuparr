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
from concurrent.futures import TimeoutError as SendTimeout
from tempfile import SpooledTemporaryFile

SPOOL_IN_MEMORY = 1024 * 1024
# A download's read size. Each chunk crosses from the worker thread to the
# event loop and back, so Werkzeug's own 8 KB made a large download CPU-bound
# (about 100 MB/s, against 3 GB/s at 1 MiB, on a desktop).
FILE_BLOCK = 1024 * 1024
# How long one chunk of a response may wait to go out. A client that stops
# reading - or a connection that stalls - otherwise holds its worker thread
# for good, and with every worker held the web UI stops answering.
SEND_TIMEOUT = 300


class ClientGone(Exception):
    pass


def new_executor(threads=16):
    return ThreadPoolExecutor(max_workers=threads, thread_name_prefix="web")


class FileWrapper:
    """PEP 3333's wsgi.file_wrapper, which Flask's send_file uses: the file
    in FILE_BLOCK reads, whatever smaller size the app asks for."""

    def __init__(self, file, block_size=FILE_BLOCK):
        self.file = file
        self.block_size = max(block_size, FILE_BLOCK)
        if hasattr(file, "close"):
            self.close = file.close

    def __iter__(self):
        return self

    def __next__(self):
        data = self.file.read(self.block_size)
        if data:
            return data
        raise StopIteration


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
        # The whole body is in `body` already, so reading to its end is
        # safe: without this, Werkzeug reads a chunked body as empty.
        "wsgi.input_terminated": True,
        "wsgi.file_wrapper": FileWrapper,
        "wsgi.errors": sys.stderr,
        "wsgi.multithread": True,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    if scope.get("client"):
        environ["REMOTE_ADDR"] = scope["client"][0]
    for raw_name, raw_value in scope.get("headers", []):
        name = raw_name.decode("latin-1").lower()
        if "_" in name:
            # X_Forwarded_For would land on X-Forwarded-For's key and, sent
            # after it, pass for the proxy's value; dropped, as Waitress,
            # nginx and Apache do.
            continue
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


def _declared_length(scope):
    for name, value in scope.get("headers", []):
        if name.lower() == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


class WsgiToAsgi:
    """max_body: the biggest request body, in bytes, or a function of the
    request's path that gives it. The body is read before the app (and its
    login check) sees the request, so it's kept small where it can be.
    executor: the thread pool requests run in, shared by the servers that
    pass the same one; without it this adapter makes its own."""

    def __init__(self, app, max_body, executor=None, send_timeout=SEND_TIMEOUT):
        self.app = app
        self.send_timeout = send_timeout
        self.max_body = max_body if callable(max_body) else (lambda _path: max_body)
        self.executor = executor or new_executor()

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
        limit = self.max_body(scope["path"])
        declared = _declared_length(scope)
        if declared is not None and declared > limit:
            await _plain(send, 413, b"Request too large")  # refused before a byte of it is read
            return
        body = SpooledTemporaryFile(max_size=SPOOL_IN_MEMORY)
        handed_over = False
        try:
            size = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > limit:
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
            # From here the worker owns the body and closes it when it's done:
            # cancelled at shutdown, this coroutine stops waiting, but the app
            # may still be reading it.
            handed_over = True
            try:
                await loop.run_in_executor(self.executor, self._run, build_environ(scope, body), send, loop, gone)
            finally:
                gone.set()  # cancelled: the worker stops at its next chunk
                watcher.cancel()
        finally:
            if not handed_over:
                body.close()

    def _run(self, environ, send, loop, gone):
        """Runs the app in a pool thread."""
        state = {"start": None, "sent": False}

        def blocking_send(message):
            if gone.is_set():
                raise ClientGone
            future = asyncio.run_coroutine_threadsafe(send(message), loop)
            try:
                future.result(timeout=self.send_timeout)
            except SendTimeout:
                future.cancel()  # nothing more goes to a client that isn't reading
                raise ClientGone from None
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

        try:
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
        finally:
            environ["wsgi.input"].close()
