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
    try:
        with open(args.file, "rb") as f:
            head = f.read(len(archive_crypto.MAGIC))
    except FileNotFoundError:
        sys.exit(f"decrypt: {args.file} not found")
    except OSError as exc:
        sys.exit(f"decrypt: {args.file} can't be read ({exc.strerror})")
    if not archive_crypto.is_encrypted_bytes(head):
        if archive_crypto.is_pre_3_self_backup(head):
            sys.exit(f"decrypt: {archive_crypto.PRE_3_SELF_BACKUP}")
        sys.exit("decrypt: this isn't an encrypted Backuparr backup")
    key = _key()
    try:
        archive_crypto.decrypt_file(args.file, output, key)
    except (archive_crypto.ArchiveCryptoError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, archive_crypto.ArchiveCryptoError) else "the key doesn't open it, or it isn't a Backuparr backup"
        sys.exit(f"decrypt: {reason}")
    print(f"decrypted to {output}")


if __name__ == "__main__":
    main()
