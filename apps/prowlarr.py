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
