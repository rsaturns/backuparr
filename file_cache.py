"""A file's parsed contents kept in memory, read again only when the file
changes (#231): the backup record and the run logs are read on every History
request and on every change to them."""
import os


def stamp(path):
    """Changes whenever the file at path does - its modified time, size or
    inode (Backuparr's own writes replace the file, so a new inode); None
    while it doesn't exist."""
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        return None
    return stat.st_mtime_ns, stat.st_size, stat.st_ino


class ParsedFile:
    """One file's parsed contents. The caller holds its own lock around
    every call, and must not change what get() returns except to put() it
    straight back."""

    def __init__(self):
        self._key = None
        self._value = None

    def get(self, path, load):
        """The parsed file: load() is called only when it changed since the
        last get() or put(). A missing file isn't kept."""
        key = (path, stamp(path))  # taken first: a change while loading is read next time
        if key[1] is not None and key == self._key:
            return self._value
        value = load()
        self._key, self._value = (key, value) if key[1] is not None else (None, None)
        return value

    def put(self, path, value):
        """value was just written to path."""
        key = (path, stamp(path))
        self._key, self._value = (key, value) if key[1] is not None else (None, None)

    def forget(self):
        """The next get() reads the file: after a failed write, say, when
        the value in memory may hold changes the file doesn't."""
        self._key, self._value = None, None
