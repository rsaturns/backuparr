from .servarr import ServarrApp


class LidarrApp(ServarrApp):
    # Lidarr is a Servarr fork with the same system/backup, command and
    # restore/upload endpoints as Radarr and Sonarr, on API v1 like Prowlarr
    # (Lidarr.Api.V1 BackupController, checked against Lidarr 3.1).
    api_version = "v1"

    def __init__(self, url, api_key, **kwargs):
        super().__init__("lidarr", url, api_key, **kwargs)
