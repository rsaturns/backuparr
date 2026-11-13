"""Backuparr's web server (#177): HTTPS on WEBUI_HTTPS_PORT (8991) always,
and plain HTTP on WEBUI_PORT (8990) unless HTTPS only is on.

Both run in this one process, which the scheduler and the backup/restore
lock need, as two Hypercorn servers in one asyncio loop: TLS handshakes
never hold up other visitors, and the HTTP server can be stopped and
started on its own when HTTPS only is switched (webui/https.py). Flask runs
under them through webui/wsgi_asgi.py, in a pool of threads. HTTPS speaks
HTTP/1.1.

Each server tags its requests with the port they came in on
(environ["backuparr.listener"]), which HTTPS only checks: unlike
request.is_secure, a proxy's X-Forwarded-Proto can't fake it.

Run with `python -m webui.serve` (entrypoint.sh)."""
import asyncio
import logging
import os
import signal
import ssl

from hypercorn.asyncio import serve
from hypercorn.config import Config

import tls
from config_store import ConfigError, load_config
from webui.forwarded import ForwardedHeaders
from webui.wsgi_asgi import WsgiToAsgi

log = logging.getLogger("backuparr.webui")

LISTENER_KEY = "backuparr.listener"
# Biggest request body: Flask's own limit (64 MiB, Backuparr's own backup
# uploaded as base64 to restore it) plus room for the form around it.
MAX_BODY = 80 * 1024 * 1024
RENEW_CHECK_SECONDS = 12 * 3600

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def host():
    return os.environ.get("WEBUI_HOST") or "0.0.0.0"


def http_port():
    return int(os.environ.get("WEBUI_PORT") or 8990)


def https_port():
    return int(os.environ.get("WEBUI_HTTPS_PORT") or 8991)


def https_only_env():
    """BACKUPARR_HTTPS_ONLY as True or False, or None when unset."""
    value = os.environ.get("BACKUPARR_HTTPS_ONLY", "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    if value:
        log.warning("BACKUPARR_HTTPS_ONLY=%r isn't true or false; using the Settings switch", value)
    return None


def https_only(cfg=None):
    """Whether HTTP is closed: the environment variable, else the switch."""
    env = https_only_env()
    if env is not None:
        return env
    return bool((cfg if cfg is not None else load_config()).get("https_only"))


def _https_only_at_start():
    """https_only(), or off when config.json can't be read: the web UI then
    opens on both ports and says what's wrong, as it did before #177."""
    try:
        return https_only()
    except (ConfigError, OSError) as exc:
        log.error("can't read the HTTPS only switch - %s. HTTP on port %s stays open until it's fixed.",
                  exc, http_port())
        return False


def _bind(port):
    address = host()
    return f"[{address}]:{port}" if ":" in address else f"{address}:{port}"


def _tagged(app, listener):
    def tagged(environ, start_response):
        environ[LISTENER_KEY] = listener
        return app(environ, start_response)
    return tagged


class _Config(Config):
    """Hypercorn's settings, with our own SSL context: it's kept across
    certificate changes, so a renewed or restored certificate is used
    without a restart."""
    ssl_context = None

    @property
    def ssl_enabled(self):
        return self.ssl_context is not None

    def create_ssl_context(self):
        return self.ssl_context


def _config(port, ssl_context=None):
    config = _Config()
    config.ssl_context = ssl_context
    config.bind = [_bind(port)]
    config.accesslog = None
    config.errorlog = log
    config.include_server_header = False
    config.graceful_timeout = 3
    return config


def _ssl_context(cert):
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_alpn_protocols(["http/1.1"])
    context.load_cert_chain(cert.cert_path, cert.key_path)
    return context


class Server:
    """The two servers, and switching the HTTP one on and off."""

    def __init__(self, app):
        self.app = ForwardedHeaders(app)
        self.loop = None
        self.stopping = None
        self.http_stop = None
        self.http_task = None
        self.http_wanted = False  # the latest switch, even while the old server is still closing
        self.ssl_context = None
        self.fingerprint = None

    # Called from request threads.
    def http_open(self):
        return self.http_task is not None and not self.http_task.done()

    def set_http_open(self, open_):
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(self._open_http if open_ else self._close_http)

    # In the loop.
    def _open_http(self):
        self.http_wanted = True
        if self.http_open():
            if self.http_stop.is_set():  # still closing: open again once it has
                self.http_task.add_done_callback(self._reopen_http)
            return
        self.http_stop = asyncio.Event()
        self.http_task = self.loop.create_task(
            self._run("HTTP", http_port(), None, self.http_stop))

    def _reopen_http(self, task):
        if self.http_wanted and task is self.http_task and not self.stopping.is_set():
            self._open_http()

    def _close_http(self):
        self.http_wanted = False
        if self.http_open() and not self.http_stop.is_set():
            log.info("HTTP on port %s closed: HTTPS only", http_port())
            self.http_stop.set()

    async def _run(self, name, port, ssl_context, stop):
        app = WsgiToAsgi(_tagged(self.app, "https" if ssl_context else "http"), MAX_BODY)
        try:
            await serve(app, _config(port, ssl_context), mode="asgi", shutdown_trigger=lambda: self._either(stop))
        except OSError as exc:
            log.error("can't serve %s on port %s: %s", name, port, exc)
            if ssl_context is not None:
                raise  # without HTTPS there may be no way in at all

    async def _either(self, stop):
        """Until the process stops, or `stop` is set."""
        waits = [asyncio.create_task(self.stopping.wait()), asyncio.create_task(stop.wait())]
        _, pending = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()

    def _use_certificate(self, cert):
        """tls.on_change callback: any thread."""
        self.loop.call_soon_threadsafe(self._load, cert)

    def _load(self, cert):
        if cert.fingerprint == self.fingerprint:
            return
        self.ssl_context.load_cert_chain(cert.cert_path, cert.key_path)
        self.fingerprint = cert.fingerprint
        log.info("HTTPS certificate loaded (SHA-256 %s)", cert.fingerprint)

    async def _renew(self):
        """Renews Backuparr's certificate when due, and picks up your own
        certificate's files when they're replaced."""
        while True:
            await asyncio.sleep(RENEW_CHECK_SECONDS)
            try:
                self._load(await asyncio.to_thread(tls.ensure))
            except Exception:
                log.exception("couldn't check the HTTPS certificate")

    async def main(self):
        self.loop = asyncio.get_running_loop()
        self.stopping = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self.loop.add_signal_handler(sig, self.stopping.set)
            except (ValueError, RuntimeError):  # not the main thread (tests)
                pass
        cert = tls.ensure()
        self.fingerprint = cert.fingerprint
        self.ssl_context = _ssl_context(cert)
        tls.on_change(self._use_certificate)
        https_task = asyncio.create_task(self._run("HTTPS", https_port(), self.ssl_context, asyncio.Event()))
        if _https_only_at_start():
            log.info("HTTPS only: HTTP on port %s stays closed", http_port())
        else:
            self._open_http()
        renew = asyncio.create_task(self._renew())
        await https_task
        self.stopping.set()
        renew.cancel()
        if self.http_task is not None:
            await asyncio.gather(self.http_task, return_exceptions=True)


SERVER = None


def main():
    global SERVER
    from webui import runs
    from webui.app import create_app
    app = create_app()
    # The HTTPS key and certificate first: the first start of 3.0 backs
    # Backuparr up straight away (#162), and its backup holds them.
    tls.ensure()
    runs.start_upgrade_self_backup()
    SERVER = Server(app)
    asyncio.run(SERVER.main())


def http_open():
    return SERVER is not None and SERVER.http_open()


def set_http_open(open_):
    if SERVER is not None:
        SERVER.set_http_open(open_)


if __name__ == "__main__":
    # Run as `python -m webui.serve`, this file is __main__; the routes import
    # webui.serve. Start the server in that module, so they share SERVER.
    from webui import serve as _module
    _module.main()
