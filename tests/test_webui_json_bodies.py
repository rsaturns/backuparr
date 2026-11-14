"""Every POST route takes a JSON object: anything else is a 400, never a
500 (#226)."""
import pytest

from conftest import PASSWORD


# The routes that read fields from the body with .get().
OBJECT_ROUTES = [
    "/api/secrets-key/acknowledge", "/api/backuparr/restore", "/api/reset",
    "/api/history/local/delete", "/api/history/local/download", "/api/restore/local/sabnzbd/preview",
    "/api/restore/local/radarr", "/api/restore/local/radarr/check",
    "/api/trust-certificate/opnsense", "/api/test-notify",
    "/api/test-destination/local", "/api/destinations/gdrive/folder", "/api/destinations/onedrive/connect",
    "/api/destinations/dropbox/connect", "/api/https/only", "/api/backup/run", "/api/test/radarr", "/api/config",
]


@pytest.mark.parametrize("path", OBJECT_ROUTES)
@pytest.mark.parametrize("body", [[1], [], "text", 5, True])
def test_a_body_that_isnt_an_object_is_refused(authed_client, isolated_webui, path, body):
    response = authed_client.post(path, json=body)
    assert response.status_code == 400
    error = response.get_json()
    assert "expected a JSON object" in (error.get("error") or error.get("message"))
    assert not isolated_webui.runs.RUN_STATE["running"]


def test_no_post_route_fails_on_a_json_array(isolated_webui, authed_client):
    names = {"<dest_id>": "local", "<app_name>": "radarr", "<job_id>": "x", "<app_id>": "radarr", "<filename>": "a.zip",
             "<run_id>": "radarr_20261101_030000"}
    paths = set()
    for rule in isolated_webui.app.url_map.iter_rules():
        if rule.rule.startswith("/api/") and "POST" in rule.methods:
            path = rule.rule
            for name, value in names.items():
                path = path.replace(name, value)
            paths.add(path)
    assert "/api/reset" in paths and "/api/history/local/delete" in paths
    for path in sorted(paths - {"/api/logout"}):
        client = isolated_webui.app.test_client()
        assert client.post("/api/login", json={"username": "admin", "password": PASSWORD}).status_code == 200
        response = client.post(path, json=[1])
        assert response.status_code < 500, (path, response.get_json())
        assert not isolated_webui.runs.RUN_STATE["running"], path


@pytest.mark.parametrize("folder_id", ["a b", "id\\nclient_secret = x", "../x", ["1AbC"], 5, "x" * 257])
def test_a_drive_folder_id_uses_only_the_characters_drive_ids_use(authed_client, isolated_webui, folder_id):
    response = authed_client.post("/api/destinations/gdrive/folder", json={"folder_id": folder_id, "folder_name": "Backups"})
    assert response.status_code == 400 and "folder_id" in response.get_json()["error"]
    assert not isolated_webui.backups.load_config()["destinations"]["gdrive"].get("folder_id")


def test_a_drive_folder_name_must_be_text(authed_client):
    response = authed_client.post("/api/destinations/gdrive/folder", json={"folder_id": "1AbC-d_E", "folder_name": ["x"]})
    assert response.status_code == 400 and response.get_json() == {"error": "folder_name must be text"}


def test_a_destination_test_checks_its_fields_are_text(authed_client):
    response = authed_client.post("/api/test-destination/local", json={"path": ["/backups"]})
    assert response.status_code == 400 and response.get_json() == {"ok": False, "message": "local: path must be text"}
