"""Backuparr's release version, baked into the image at build time."""
import os


# Read once - baked in at build time, can't change at runtime.
_VERSION_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")
try:
    with open(_VERSION_PATH) as _f:
        VERSION = _f.read().strip()
except OSError:
    VERSION = "dev"
