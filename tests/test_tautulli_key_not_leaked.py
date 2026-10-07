import http.server
import io
import logging
import threading

import pytest
import requests

import backup
import destination_util
from apps.tautulli import TautulliApp

KEY = "SECRET-TAUTULLI-KEY"
DEAD_URL = "http://127.0.0.1:1"  # nothing listens here


def raised(call):
    with pytest.raises(requests.exceptions.RequestException) as caught:
        call()
    return caught.value


def test_connection_errors_do_not_carry_the_api_key():
    error = raised(lambda: TautulliApp(DEAD_URL, KEY, timeout=2).test_connection())
    assert KEY not in str(error)
    assert isinstance(error, requests.exceptions.ConnectionError)
    assert error.__suppress_context__ and error.__cause__ is None


def test_timeouts_keep_their_class_without_the_key(monkeypatch):
    app = TautulliApp("http://tautulli.example", KEY)

    def slow(*args, **kwargs):
        raise requests.exceptions.ReadTimeout(f"timed out for http://tautulli.example/api/v2?apikey={KEY}")

    monkeypatch.setattr(app.session, "request", slow)
    error = raised(app.test_connection)
    assert isinstance(error, requests.exceptions.Timeout) and KEY not in str(error)
    assert backup.humanize_error(error).startswith("couldn't connect")


def test_uploads_do_not_leak_the_key_either(tmp_path):
    (tmp_path / "config.ini").write_text("[General]\n")
    error = raised(lambda: TautulliApp(DEAD_URL, KEY, timeout=2).restore(str(tmp_path)))
    assert KEY not in str(error)


def test_a_failed_backup_run_keeps_the_key_out_of_the_logs(tmp_path, monkeypatch):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger = logging.getLogger("backuparr")
    logger.addHandler(handler)
    monkeypatch.setattr(backup, "enabled_apps", lambda cfg: ["tautulli"])
    monkeypatch.setattr(backup, "enabled_destinations", lambda cfg: ["local"])
    monkeypatch.setattr(destination_util, "sync", lambda cfg: None)
    monkeypatch.setattr(destination_util, "remote_root", lambda dest_id, cfg: str(tmp_path))
    monkeypatch.setattr(backup.rclone_util, "delete_older_than", lambda *args: None)
    cfg = {"retention_days": 7, "apps": {"tautulli": {"url": DEAD_URL, "api_key": KEY}}, "destinations": {"local": {}}}
    try:
        ok, failed = backup.run_backup(cfg)
    finally:
        logger.removeHandler(handler)

    assert not ok and len(failed) == 1
    assert "backup failed - couldn't connect - connection refused by 127.0.0.1:1" in stream.getvalue()
    assert "Traceback" not in stream.getvalue()  # an unreachable app is reported in one line
    assert KEY not in stream.getvalue()
    assert KEY not in "".join(failed)


@pytest.fixture
def server_answering_500():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(500)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def logged(call):
    stream = io.StringIO()
    logger = logging.getLogger("backuparr.test")
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)
    try:
        call()
    except Exception:
        logger.exception("failed")
    finally:
        logger.removeHandler(handler)
    return stream.getvalue()


def test_an_http_error_response_does_not_carry_the_key_into_the_log(server_answering_500):
    # requests' own HTTPError text repeats the whole URL, apikey included.
    output = logged(lambda: TautulliApp(server_answering_500, KEY).test_connection())
    assert "HTTP 500" in output and KEY not in output


def test_an_http_error_while_importing_does_not_carry_the_key_into_the_log(server_answering_500, tmp_path):
    (tmp_path / "config.ini").write_text("[General]\n")
    output = logged(lambda: TautulliApp(server_answering_500, KEY).restore(str(tmp_path)))
    assert "HTTP 500" in output and KEY not in output
