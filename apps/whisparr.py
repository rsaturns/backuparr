from .servarr import ServarrApp


class WhisparrApp(ServarrApp):
    # Whisparr v2 (a Sonarr fork) and v3 (a Radarr fork) both serve the same
    # system/backup, command and restore/upload endpoints as Radarr and Sonarr
    # under API v3 (Whisparr.Api.V3 BackupController on the v2-develop and eros
    # branches), so one driver covers both.
    api_version = "v3"

    def __init__(self, url, api_key, **kwargs):
        super().__init__("whisparr", url, api_key, **kwargs)
