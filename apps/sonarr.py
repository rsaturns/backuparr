from .servarr import ServarrApp


class SonarrApp(ServarrApp):
    api_version = "v3"

    def __init__(self, url, api_key, **kwargs):
        super().__init__("sonarr", url, api_key, **kwargs)
