# VEX: findings that don't affect Backuparr

[`backuparr.vex.json`](backuparr.vex.json) is an [OpenVEX](https://openvex.dev/)
document. It lists vulnerabilities that scanners report in Backuparr's image
but that don't affect Backuparr, each with the reason (`impact_statement`):

| Finding | In | Why it doesn't affect Backuparr |
|---|---|---|
| CVE-2026-97054 | Go's `net/http`, in rclone | An HTTP/1 server crash; Backuparr runs rclone only as a client |
| CVE-2026-128085, CVE-2026-128086 | OpenTelemetry, in rclone | Tracing code; rclone records and serves no traces here |
| GO-2026-5932 | `golang.org/x/crypto/openpgp`, in rclone | Used by rclone backends Backuparr doesn't use |
| CVE-2026-119586 | SQLite | The `sqlite3` shell's `.recover`; the image has only the library |
| CVE-2026-107891 | musl | Already fixed in Alpine 3.24's musl 1.2.6-r3, which the image has |

## Checking an image against it

[Trivy](https://trivy.dev/):

```sh
trivy image --vex vex/backuparr.vex.json rsaturns/backuparr:3.0.0
```

[Docker Scout](https://docs.docker.com/scout/) only accepts statements from
the authors you allow, and only hides `not_affected` findings when asked:

```sh
docker scout cves registry://rsaturns/backuparr:3.0.0 \
  --vex-location vex/ --vex-author '.*' --only-vex-affected
```

Add `--platform linux/arm64` for the arm64 image.

## Keeping it current

- Trivy matches the image by its repository (`pkg:oci/...`), so those
  statements cover every version until the package is updated.
- Docker Scout matches each platform's image digest
  (`pkg:docker/rsaturns/backuparr@sha256:...`). A new release needs its
  amd64 and arm64 digests added for the statements that still apply.
- When rclone, SQLite or musl is updated and a finding goes away, remove
  its statement and raise `version`.
