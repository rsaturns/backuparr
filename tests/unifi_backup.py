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


def make_unifi(firmware="5.1.42", network="10.6.109", members=None, cut=0):
    """A UniFi OS .unifi backup as UniFi OS writes it: a random IV, then
    AES-256-CBC (PKCS#7 padding) of a gzipped tar. `cut` drops that many bytes
    of the gzip's end, as a damaged download would."""
    import json
    import os
    import tarfile

    files = {"backup/metadata.json": json.dumps({"firmware_version": firmware, "hardware_shortname": "UOSSERVER"}).encode(),
             "backup/package-versions.json": json.dumps({"controllers": {"network": f"{network}-36013-1"},
                                                         "backupTarget": "all"}).encode(),
             "backup/network/db.gz": gzip.compress(b"\x30\x00\x00\x00\x02__cmd\x00select"),
             "backup/network/version": network.encode(),
             "backup/ucore/config/settings.yaml": b"name: UOS Server\n"}
    files.update(members or {})
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, data in files.items():
            if data is not None:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    plain = gzip.compress(raw.getvalue())
    plain = plain[:len(plain) - cut] if cut else plain
    pad = 16 - len(plain) % 16  # PKCS#7, as UniFi OS pads
    plain += bytes([pad]) * pad
    iv = os.urandom(16)
    key = bytes.fromhex("e383b7c53698b36d4baea4ed22181ef73676bfd5d5b90005d9845ffd5dce985f")
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return iv + encryptor.update(plain) + encryptor.finalize()
