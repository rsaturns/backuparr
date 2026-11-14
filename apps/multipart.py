"""A streamed multipart/form-data upload body."""
import io
import os
import secrets


class MultipartFile:
    """A multipart/form-data body holding one file, read from disk while
    it's sent: requests' files= would build the whole body in memory, and
    a backup can be gigabytes. Pass it as data= with its content_type as
    the Content-Type header, and close() it afterwards."""

    def __init__(self, path, field, filename, content_type):
        boundary = secrets.token_hex(16)
        self.content_type = f"multipart/form-data; boundary={boundary}"
        head = (f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n").encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        self._length = len(head) + os.path.getsize(path) + len(tail)
        self._file = open(path, "rb")
        self._parts = [io.BytesIO(head), self._file, io.BytesIO(tail)]

    def __len__(self):
        return self._length

    def read(self, size=-1):
        out = b""
        while self._parts and (size < 0 or len(out) < size):
            chunk = self._parts[0].read(-1 if size < 0 else size - len(out))
            if chunk:
                out += chunk
            else:
                self._parts.pop(0)
        return out

    def close(self):
        self._file.close()
