from .servarr import ServarrApp


class RadarrApp(ServarrApp):
    api_version = "v3"

    def __init__(self, url, api_key, **kwargs):
        super().__init__("radarr", url, api_key, **kwargs)
