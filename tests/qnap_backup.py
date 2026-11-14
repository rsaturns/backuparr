"""A file shaped like QNAP's system configuration file (#169), for tests."""
import struct

from apps import qnap


def make_bin(size=4096, encrypted=1024):
    """Encrypted-looking bytes, then QNAP's 74-byte footer: "icpnas", the
    encrypted length (little-endian), and the empty model and version."""
    footer = b"icpnas" + struct.pack("<I", encrypted)
    footer += b"\0" * (qnap.FOOTER - len(footer))
    return bytes((i * 7 + 3) % 251 for i in range(size - qnap.FOOTER)) + footer
