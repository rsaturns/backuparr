"""The WSGI-to-ASGI adapter Backuparr runs Flask through (#177)."""
import asyncio
import sys
import threading
import time

import pytest

from webui.wsgi_asgi import WsgiToAsgi, build_environ


def scope(**extra):
    return {"type": "http", "method": "GET", "path": "/x", "query_string": b"a=1", "http_version": "1.1",
            "scheme": "https", "server": ("127.0.0.1", 8991), "client": ("10.0.0.5", 50000),
            "headers": [(b"host", b"backuparr")], **extra}


async def call(adapter, body=b"", chunks=None, disconnect_after=None, **scope_extra):
    """Runs one request; (messages sent, environ the app saw)."""
    chunks = chunks or [body]
    incoming = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]
    sent = []
    disconnect = asyncio.Event()

    async def receive():
        if incoming:
            return incoming.pop(0)
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if disconnect_after is not None and len(sent) >= disconnect_after:
            disconnect.set()
        await asyncio.sleep(0)

    await adapter(scope(**scope_extra), receive, send)
    return sent


def run(coro):
    return asyncio.run(coro)


def body_of(sent):
    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def test_an_empty_body_still_sends_its_headers():
    # Flask's 304 Not Modified: Hypercorn's own wrapper failed this with a 500.
    def app(environ, start_response):
        start_response("304 Not Modified", [("ETag", '"x"')])
        return []
    sent = run(call(WsgiToAsgi(app, 100)))
    assert sent[0] == {"type": "http.response.start", "status": 304, "headers": [(b"etag", b'"x"')]}
    assert sent[-1] == {"type": "http.response.body", "body": b"", "more_body": False}


def test_a_generator_app_may_call_start_response_late():
    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        yield b"hello"
    sent = run(call(WsgiToAsgi(app, 100)))
    assert sent[0]["status"] == 200 and body_of(sent) == b"hello"


def test_headers_can_still_be_swapped_for_an_error_before_the_body():
    def app(environ, start_response):
        start_response("200 OK", [])
        try:
            raise ValueError("boom")
        except ValueError:
            start_response("500 Internal Server Error", [], sys.exc_info())
        return [b"error"]
    sent = run(call(WsgiToAsgi(app, 100)))
    assert sent[0]["status"] == 500 and body_of(sent) == b"error"


def test_start_response_twice_without_exc_info_is_refused():
    def app(environ, start_response):
        start_response("200 OK", [])
        start_response("200 OK", [])
        return [b""]
    with pytest.raises(RuntimeError):
        run(call(WsgiToAsgi(app, 100)))


def test_close_always_runs():
    closed = []

    class Body:
        def __iter__(self):
            yield b"a"
            raise OSError("disk went away")

        def close(self):
            closed.append(True)

    def app(environ, start_response):
        start_response("200 OK", [])
        return Body()
    with pytest.raises(OSError):
        run(call(WsgiToAsgi(app, 100)))
    assert closed == [True]


def test_the_request_body_reaches_the_app_and_too_much_gets_413():
    seen = []

    def app(environ, start_response):
        seen.append(environ["wsgi.input"].read())
        start_response("200 OK", [])
        return [b"ok"]
    run(call(WsgiToAsgi(app, 10), chunks=[b"12345", b"6789"]))
    assert seen == [b"123456789"]
    sent = run(call(WsgiToAsgi(app, 10), chunks=[b"123456", b"78901"]))
    assert sent[0]["status"] == 413 and seen == [b"123456789"]


def test_the_body_limit_can_depend_on_the_path():
    seen = []

    def app(environ, start_response):
        seen.append(environ["wsgi.input"].read())
        start_response("200 OK", [])
        return [b"ok"]
    adapter = WsgiToAsgi(app, lambda path: 100 if path == "/upload" else 4)
    assert run(call(adapter, chunks=[b"12345"], path="/upload"))[0]["status"] == 200
    assert run(call(adapter, chunks=[b"12345"], path="/x"))[0]["status"] == 413
    assert seen == [b"12345"]


def test_a_declared_length_over_the_limit_is_refused_before_reading():
    reads = []

    async def main():
        async def receive():
            reads.append(1)
            return {"type": "http.request", "body": b"x" * 10, "more_body": False}
        sent = []

        async def send(message):
            sent.append(message)
        await WsgiToAsgi(lambda e, s: [], 4)(scope(headers=[(b"content-length", b"999999")]), receive, send)
        return sent
    sent = run(main())
    assert sent[0]["status"] == 413 and not reads


def test_a_chunked_body_reaches_flask():
    # No Content-Length: a script streaming its body (Transfer-Encoding: chunked).
    from flask import Flask, request
    flask_app = Flask(__name__)
    flask_app.post("/x")(lambda: request.get_data())
    sent = run(call(WsgiToAsgi(flask_app.wsgi_app, 100), chunks=[b'{"a":', b"1}"], method="POST",
                    headers=[(b"host", b"backuparr"), (b"transfer-encoding", b"chunked")]))
    assert sent[0]["status"] == 200 and body_of(sent) == b'{"a":1}'


def test_environ_follows_pep_3333():
    environ = build_environ(scope(headers=[(b"cookie", b"a=1"), (b"cookie", b"b=2"), (b"x-thing", b"1"),
                                           (b"x-thing", b"2"), (b"content-type", b"application/json"),
                                           (b"content-length", b"2")]), None)
    assert environ["SERVER_PORT"] == "8991" and environ["REMOTE_ADDR"] == "10.0.0.5"
    assert environ["wsgi.url_scheme"] == "https" and environ["QUERY_STRING"] == "a=1"
    assert environ["HTTP_COOKIE"] == "a=1; b=2" and environ["HTTP_X_THING"] == "1,2"
    assert environ["CONTENT_TYPE"] == "application/json" and environ["CONTENT_LENGTH"] == "2"
    # Non-ASCII paths arrive as the bytes' latin-1 reading, as WSGI expects.
    assert build_environ(scope(path="/é"), None)["PATH_INFO"] == "/é".encode("utf-8").decode("latin-1")


def test_a_file_download_goes_in_large_blocks(tmp_path):
    # Flask's send_file asks wsgi.file_wrapper for 8 KB reads; each chunk is
    # a trip between threads, so the adapter's wrapper reads 1 MiB.
    from flask import Flask, send_file
    path = tmp_path / "backup.zip"
    path.write_bytes(b"x" * (3 * 1024 * 1024 + 5))
    flask_app = Flask(__name__)
    flask_app.get("/x")(lambda: send_file(path, as_attachment=True))
    sent = run(call(WsgiToAsgi(flask_app.wsgi_app, 100)))
    chunks = [m for m in sent if m["type"] == "http.response.body" and m.get("body")]
    assert body_of(sent) == path.read_bytes()
    assert len(chunks) == 4  # three full blocks and the last 5 bytes


def test_headers_with_underscores_are_dropped():
    # A Go proxy (Caddy, Traefik) sends X_Forwarded_For after its own
    # X-Forwarded-For; it must not reach the same WSGI key.
    environ = build_environ(scope(headers=[(b"x-forwarded-for", b"10.0.0.1"), (b"x_forwarded_for", b"1.2.3.4"),
                                           (b"x_thing", b"1")]), None)
    assert environ["HTTP_X_FORWARDED_FOR"] == "10.0.0.1"
    assert "HTTP_X_THING" not in environ


def test_a_download_stops_when_the_browser_goes_away():
    produced = []

    def app(environ, start_response):
        start_response("200 OK", [])
        for i in range(1000):
            produced.append(i)
            yield b"x" * 1024
    run(call(WsgiToAsgi(app, 100), disconnect_after=5))
    assert len(produced) < 50


def test_a_request_cancelled_at_shutdown_stops_and_can_still_read_its_body():
    started, done = threading.Event(), threading.Event()
    seen = {}

    def app(environ, start_response):
        started.set()
        time.sleep(0.2)  # still running when the server gives up on it
        seen["body"] = environ["wsgi.input"].read()
        start_response("200 OK", [])

        def chunks():
            try:
                for i in range(1000):
                    seen["chunks"] = i
                    yield b"x" * 1024
            finally:
                done.set()
        return chunks()

    async def main():
        adapter = WsgiToAsgi(app, 100)
        task = asyncio.create_task(call(adapter, body=b"upload"))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()  # Hypercorn's graceful timeout ran out
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.to_thread(done.wait, 5)
    run(main())
    assert seen["body"] == b"upload" and seen["chunks"] == 0


def test_requests_run_side_by_side():
    gate = threading.Event()

    def app(environ, start_response):
        if environ["PATH_INFO"] == "/slow":
            gate.wait(5)
        start_response("200 OK", [])
        return [environ["PATH_INFO"].encode()]

    async def both():
        adapter = WsgiToAsgi(app, 100)
        slow = asyncio.create_task(call(adapter, path="/slow"))
        started = time.monotonic()
        fast = await call(adapter, path="/fast")
        elapsed = time.monotonic() - started
        gate.set()
        await slow
        return body_of(fast), elapsed
    body, elapsed = run(both())
    assert body == b"/fast" and elapsed < 2


def test_lifespan_is_answered():
    async def go():
        messages = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
        sent = []

        async def receive():
            return messages.pop(0)

        async def send(message):
            sent.append(message["type"])
        await WsgiToAsgi(lambda e, s: [], 100)({"type": "lifespan"}, receive, send)
        return sent
    assert run(go()) == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
