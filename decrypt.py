"""Decrypts a Backuparr backup without a running Backuparr (#147):

    docker run --rm -it --user "$(id -u):$(id -g)" -v "$PWD:/work" \
        rsaturns/backuparr decrypt /work/radarr_20261108_030000.zip.enc

writes the plain archive next to it (radarr_20261108_030000.zip). The key
comes from BACKUPARR_SECRETS_KEY (pass it with -e), or is asked for. In a
running container, `docker exec -it backuparr python /app/decrypt.py <file>`
uses the install's own key. Backuparr's own backup
(backuparr_<timestamp>.enc) becomes a .zip; the secrets in its config.json
stay encrypted with the same key."""
import argparse
import getpass
import os
import sys

import archive_crypto
import secrets_crypto
from cryptography.fernet import Fernet, InvalidToken


def _key():
    """The key's text: BACKUPARR_SECRETS_KEY, the install's key file if this
    runs where one is, or typed in. Never creates a key."""
    if os.environ.get("BACKUPARR_SECRETS_KEY"):
        return os.environ["BACKUPARR_SECRETS_KEY"]
    try:
        with open(secrets_crypto.KEY_PATH, encoding="ascii") as f:
            return f.read().strip()
    except OSError:
        pass
    if not sys.stdin.isatty():
        sys.exit("decrypt: set BACKUPARR_SECRETS_KEY, or run with -it to type the key")
    return getpass.getpass("Encryption key: ")


def _output_for(path):
    name = archive_crypto.plain_name(os.path.basename(path))
    if name == os.path.basename(path):
        name += ".decrypted"
    elif "." not in name.split("_")[-1]:
        name += ".zip"  # Backuparr's own backup: backuparr_<timestamp>.enc
    return os.path.join(os.path.dirname(path), name)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="decrypt", description="Decrypt a Backuparr backup with your encryption key.")
    parser.add_argument("file", help="the encrypted backup (.enc)")
    parser.add_argument("output", nargs="?", help="where to write it (default: beside it, without .enc)")
    args = parser.parse_args(argv)
    output = args.output or _output_for(args.file)
    if os.path.exists(output):
        sys.exit(f"decrypt: {output} already exists - move it or name another output")
    key = _key()
    try:
        if archive_crypto.is_encrypted(args.file):
            archive_crypto.decrypt_file(args.file, output, key)
        else:
            # Backuparr's own backup from before 3.0: one Fernet token.
            with open(args.file, "rb") as f:
                data = Fernet(key.strip().encode("ascii")).decrypt(f.read())
            with open(output, "wb") as f:
                f.write(data)
    except FileNotFoundError:
        sys.exit(f"decrypt: {args.file} not found")
    except (archive_crypto.ArchiveCryptoError, InvalidToken, ValueError) as exc:
        reason = str(exc) if isinstance(exc, archive_crypto.ArchiveCryptoError) else "the key doesn't open it, or it isn't a Backuparr backup"
        sys.exit(f"decrypt: {reason}")
    print(f"decrypted to {output}")


if __name__ == "__main__":
    main()
