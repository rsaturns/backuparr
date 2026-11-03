---
title: Build from source
parent: Getting started
nav_order: 3
---

# Build from source

Clone the [repository](https://github.com/rsaturns/backuparr) and use its
`docker-compose.yml` (or replace the `image:` line in the [Docker](docker.md)
compose file with `build: .`):

```sh
docker compose up -d --build backuparr
```

Then go through the [first run](first-run.md).
