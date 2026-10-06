import secrets
import threading

from .servarr import ServarrApp


class ProwlarrApp(ServarrApp):
    api_version = "v1"
    # Discovery also creates a manual backup. Keep it separate from a
    # scheduled backup so neither operation downloads/deletes the other's zip.
    backup_lock = threading.Lock()

    def __init__(self, url, api_key, **kwargs):
        super().__init__("prowlarr", url, api_key, **kwargs)

    def backup(self, dest_dir):
        with self.backup_lock:
            return super().backup(dest_dir)

    def trigger_discovery_backup(self):
        # Prowlarr can return an already running Backup command. Its original
        # User-Agent survives that deduplication, so only our own command may
        # authorize deleting the resulting archive.
        marker = "BackuparrDiscovery/" + secrets.token_urlsafe(24)
        command = self.trigger_backup(request_headers={"User-Agent": marker})
        return (command.get("body") or {}).get("clientUserAgent") == marker
