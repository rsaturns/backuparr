# Security Policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Instead, use GitHub's [private vulnerability reporting](https://github.com/rsaturns/backuparr/security/advisories/new) (Security tab → Report a vulnerability). This opens a private conversation with the maintainer so a fix can be prepared before details are public.

Include what you'd include in a bug report: affected version/commit, steps to reproduce, and the impact.

## Known, accepted risks

A few things are intentionally not "fixed" - they're documented trade-offs for this project's single-trusted-admin threat model, not oversights:

- `POST /api/reset` (the forgot-password recovery flow) is reachable without authentication, gated only by a confirmation phrase visible in the client-side source. See the README's [Login](README.md#login) section.
- `rclone`'s OAuth tokens/client secrets are passed as subprocess arguments on every sync, visible to anything that can inspect the container's own process list (`/proc`, `ps`, `docker top`).

If you believe either of these has a worse impact than described, or you've found something else, please still report it privately first.

## Supported versions

This project is pre-1.0 and moves quickly - only the latest `main`/`latest` image and the most recent tagged release receive fixes. There's no long-term support branch.
