from .servarr import ServarrApp


class ProwlarrApp(ServarrApp):
    api_version = "v1"

    def __init__(self, url, api_key, **kwargs):
        super().__init__("prowlarr", url, api_key, **kwargs)
