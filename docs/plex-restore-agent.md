# Optional Plex restore agent

The agent restores the **library database**, including all users' locally stored
watched/unwatched state, progress, ratings, playlists and collections. Backuparr
uploads the selected backup over HTTP; only the agent accesses Docker and the
Plex database directory. Backup itself still uses Plex's API exclusively.

The agent stops/starts the **whole container** through Docker Engine. It never
executes inside Plex, replaces its entrypoint, or depends on an image's process
supervisor. Keep your existing Plex image and update it normally.

## Scope and prerequisites

- Linux Docker Engine and a normal, persistent Plex container (including Compose).
  Swarm tasks, auto-remove containers, Kubernetes and Windows containers are not
  supported. Do not run competing updaters/recreations during a restore.
- An initialized, running Plex server. The agent checks the container's mount,
  the mounted `Preferences.xml` machine identifier, the live API identity and
  Backuparr's configured server before stopping anything.
- A **new Backuparr backup containing its source Plex version and server ID**.
  Older archives remain manually restorable but lack the checks for automatic
  restore. Automatic restore requires the **same server identity and exact Plex
  version**, including build suffix. For a backup from before an upgrade, first
  run the matching Plex version. Cross-server migration remains manual.
- The mounted library DB must be a regular file, not a symlink/hard link. The
  agent supports bind mounts and named volumes, with no nested database mounts.
- The agent's state volume must persist across container recreation. It holds
  the upload, operation journal and rollback copies. Allow space for roughly
  **the uploaded archive, its extracted `databases.zip`, the expanded database
  and the existing database directory**, plus a temporary database copy on the
  Plex volume. HTTP uploads stream directly to the authenticated job's directory;
  the HTTP server does not keep an additional archive copy.

The agent does **not** apply exported API settings, metadata XML or artwork files;
the backup's `RESTORE.txt` describes their manual recovery. It does not restore
`Preferences.xml`, server claiming, media, subtitles, plugins or cloud-only data.
Keep the existing Plex data/configuration and media mounts. A database export is
not a complete, portable server image. Stock SQLite cannot fully integrity-check
Plex's custom tokenizer; validation checks ZIP CRCs of consumed members, the
SQLite header and required Plex tables, followed by live API readiness.
Manifest parsing streams older inline artwork lists without loading them all into
memory; new exports keep those lists in `artwork/unavailable.json`. The expanded
manifest is subject to `MAX_ARCHIVE_BYTES`, with separate bounds on individual
JSON values and nesting. Corrupt or truncated manifests are rejected before Plex
is stopped, including malformed data after the server identity.
The agent requires ijson's `yajl2_c` parser to keep memory bounded. The image
installs its binary wheel and verifies the import during build; a custom install
without this backend fails at startup. `IJSON_BACKEND` cannot select a fallback
for restore manifests.

## Compose setup

Use [plex-restore.compose.yml](plex-restore.compose.yml) as a merge example.
Merge the `plex` label into your existing service and add the agent, secret and
state volume. For a different service name, change that service key too. **Do not
change your Plex image.** Set these variables in your compose `.env`:

```dotenv
PLEX_CONTAINER=plex
PLEX_DATA_PATH=/absolute/host/path/to/Plex Media Server
PLEX_CONTAINER_DATABASE_DIR=/config/Library/Application Support/Plex Media Server/Plug-in Support/Databases
PLEX_RESTORE_TOKEN_FILE=/absolute/private/path/plex-restore-token
```

`PLEX_DATA_PATH` is the directory containing `Preferences.xml` and `Plug-in
Support`, not necessarily the root of your container's `/config` mount. Adapt
both paths to your image's existing layout. The example grants write access only
to the database directory; `Preferences.xml` is mounted read-only. With a named
Plex volume, mount that same volume in the agent and set `PLEX_DATABASE_DIR` to
its database subdirectory; the agent verifies both paths resolve to the same
Docker volume and location.

Generate the separate agent credential (never reuse your Plex token):

```sh
umask 077
openssl rand -hex 32 > /absolute/private/path/plex-restore-token
```

The source build uses `plex_restore_agent/Dockerfile`, with the repository root
as build context. **Compose resolves relative paths against the first compose
file**, including paths in merge files. Set the agent's `build.context` to the
absolute Backuparr checkout path when merging the example into another project.
You can also omit `build` and use a published agent image.

In Backuparr's Plex settings:

1. Keep the existing Plex URL and owner's Plex token. A domain is fine.
2. Set **Restore agent URL** to `http://plex-restore-agent:8991` and paste the
   generated token into **Restore agent token**. Backuparr encrypts it at rest.
3. Click **Test restore agent**, then **Save settings**.
4. Create a new backup. In **Restore**, select Plex and the backup, review the
   scope, check the existing confirmation box and click **Restore**.

The two containers must share a Docker network. The agent's `PLEX_URL` may use
a direct internal address even if Backuparr uses the public Plex domain.

### Host networking

If Plex and Backuparr use `network_mode: host`, the agent can too. Add:

```yaml
network_mode: host
environment:
  # Keep the other required variables from the example.
  AGENT_HOST: 127.0.0.1
  PLEX_URL: http://127.0.0.1:32400
```

Set Backuparr's agent URL to `http://127.0.0.1:8991`. Do not add `ports` with
host networking. The listener is then reachable only on the host's loopback.

## Recovery behavior

One operation at a time is allowed, also enforced across agents sharing the
database directory. Before replacing files, the agent:

1. Validates and stages the database without touching Plex.
2. Records the exact container ID and original restart policy on disk, disables
   automatic restarts temporarily, stops Plex and verifies it is stopped.
3. Saves the current library and blobs databases, including any WAL/SHM files,
   to a private rollback folder on the state volume.
4. Replaces only the library database, preserves its UID/GID/mode, removes its
   stale WAL/SHM files and starts Plex. Existing blobs remain unchanged.
5. Checks the same server/version and its library API, restores the original
   Docker restart policy and reports success.

If this fails, the agent stops Plex and restores **all** captured files and the
original restart policy. If the agent dies mid-operation, it rolls back from
its durable journal on startup. It never silently resumes applying an archive
after a crash. Backuparr can continue polling across that restart; a timeout
does not cancel the agent's operation.
The failure reason is recorded before rollback, alongside its journal state.
Once `rolled_back` is durably saved, startup recovery leaves that operation alone;
there is no later diagnostic write that can make it pending again.

**Keep the state volume.** Successful and failed operations retain their
`/state/<restore-id>/rollback/` folder and `job.json`. These are sensitive files
and are not automatically pruned. After verifying a restore, stop the agent
before removing old terminal job directories. Never delete a job in progress
or one marked `recovery_failed`.

If automatic rollback cannot finish (for example Docker is unavailable), new
restores are blocked and the status says `recovery_failed`. If even the journal
cannot be written, the job may still show its last saved phase. The agent still
blocks new restores and reports HTTP 503 from `/v1/health` with
`recovery_required: true`. Freeing space or manually starting Plex does not lift
this block. Fix the Docker, mount, permissions or disk problem and restart the
agent to reconcile the journal and retry recovery before accepting new restores.
If manual intervention is required, stop Plex, inspect `job.json`'s
`original_files`, copy those files from `rollback/` to the database directory,
restore the recorded ownership/mode, and remove database/WAL/SHM filenames from
the six supported names that were absent from `original_files`. If there is no
`original_files` entry, the agent had not started replacing databases. Restore
the recorded Docker restart policy only once files are consistent, then start
Plex. Preserve the journal for diagnosis.

## API and access

Every request requires `Authorization: Bearer <agent-token>`. Authentication is
checked before reading or saving the body. The HTTP server closes each connection
after its response, so rejected uploads do not have to finish before receiving
an error. Request lines and headers must finish within three seconds, including
clients that keep sending individual bytes. An admitted, authenticated upload
has a separate 900-second inactivity timeout while streaming its body; its total
duration is not limited to three seconds.
No CORS is enabled and the API accepts no container IDs, shell commands
or filesystem paths from clients. Configuration fixes the one labelled target
container at startup.

- `GET /v1/health`: agent/recovery status; does not require Plex to be running.
- `GET /v1/status`: verified target container and live Plex identity.
- `PUT /v1/restores/<32-character-hex-id>`: raw `application/zip` body, with a
  `Content-Length`, `X-Plex-Token` and `X-Plex-Machine-Identifier`. Returns 202;
  repeated requests for the same ID never apply the backup again.
- `GET /v1/restores/<id>`: durable phase/result, without credentials.

The Plex token is used in memory for preflight/readiness only. It is not written
to the job journal. On startup recovery the agent uses the unauthenticated
Plex identity endpoint after restoring the original files.

Optional environment settings: `AGENT_HOST` (default `0.0.0.0`), `AGENT_PORT`
(`8991`), `AGENT_TOKEN_FILE` (`/run/secrets/plex_restore_token`), `DOCKER_SOCKET`
(`/var/run/docker.sock`), `PLEX_DATABASE_DIR` (`/plex-data/Plug-in Support/Databases`),
`AGENT_STATE_DIR` (`/state`), `PLEX_HEALTH_TIMEOUT` (180 seconds, range 10–900),
and `MAX_ARCHIVE_BYTES` (20 GiB for upload, expanded manifest and each expanded database archive).
`AGENT_CONTAINER` defaults to Docker's container hostname/ID; set it explicitly
if you customize the hostname.

**Docker socket access grants broad control over the host.** The single-target
checks and label protect against configuration mistakes; they are not a Docker
authorization boundary. Mounting the socket `:ro` does not restrict API writes.
Do not publish this API to the internet. Keep it on a private Docker network or
host loopback, protect its token and state volume, and trust the agent image as
you would other software with Docker administration rights. Cross-host access,
if needed, must use an authenticated HTTPS proxy. Stronger daemon-side isolation
requires an external authorization mechanism that actually restricts container
IDs and methods, not merely a generic proxy permitting all container endpoints.
