"""A UniFi Network .unf backup, made the way UniFi makes it (#161): a zip,
AES-128-CBC encrypted with UniFi's shared key and no padding, so the zip's
tail past the last whole block is lost."""
import gzip
import io
import zipfile

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def make_unf(version="10.5.67", pad=0, members=None):
    """A .unf as UniFi writes it: the zip's tail past the last whole AES
    block is lost. `pad` changes the zip's length, so how much is cut."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        files = {"db.gz": gzip.compress(b"\x30\x00\x00\x00\x02__cmd\x00select"), "db_stat.gz": gzip.compress(b"stats"),
                 "system.properties": b"# system.properties\n", "timestamp": b"1794440525857" + b"0" * pad,
                 "format": b"bson", "version": version.encode()}
        files.update(members or {})
        for name, data in files.items():
            if data is not None:
                z.writestr(name, data)
    plain = buf.getvalue()
    plain = plain[:len(plain) - len(plain) % 16]
    encryptor = Cipher(algorithms.AES(b"bcyangkmluohmars"), modes.CBC(b"ubntenterpriseap")).encryptor()
    return encryptor.update(plain) + encryptor.finalize()
