# Jarvis production deployment

For the controlled local release path from `~/jarvis-dev`, see
[the deployment pipeline](deployment-pipeline.md). It replaces archive/SCP releases
with exact-commit approval, immutable image builds, checks and verified rollback.

This foundation runs one Jarvis web/API process behind an HTTPS reverse proxy.
The UI and task/model workflows are unchanged. Production adds authentication,
configuration checks, persistent storage, health checks and structured logging.
It is a single-owner service: anyone with the authentication token has access to
all conversations, memories, tasks and candidate/apply operations. It is not a
multi-tenant service, and authentication does not replace exact apply approval.

## Architecture and trust boundary

`Dockerfile` uses Python 3.14 on Debian Bookworm with pinned Python requirements,
Git, Node and Bubblewrap. It copies explicit application directories, never Git
history, local `.env`, local virtual environments or persistent databases.
`.dockerignore` additionally filters nested env files, private key formats and
credential filenames. No build arguments contain secrets. Inspect source additions
and image contents before release; filename filters cannot detect a secret someone
puts directly into ordinary source code. Release automation should pin the base
image digest and rebuild regularly for OS/package security updates.

`compose.yaml` provides one service and the `jarvis_state` named volume:

- UID/GID 10001, read-only root filesystem, all capabilities dropped,
  `no-new-privileges`, default Docker seccomp, no privileged mode.
- No Docker socket, host checkout or broad host directory mounts.
- `/var/lib/jarvis` is writable persistent state; `/tmp` is bounded ephemeral
  scratch space. Python bytecode writes are disabled.
- Only `127.0.0.1:8000` is published. Do not change this to a public bind.
- One worker, 64 concurrent connections/tasks at the server perimeter, 2 CPUs,
  1 GiB RAM, 256 PIDs and 256 MiB temporary scratch. Adjust resources based on
  measured usage without relaxing isolation. There is no horizontal SQLite HA.
- `init: true` forwards signals/reaps children. `restart: unless-stopped` restarts
  exited processes and starts the service after Docker restarts.

Resource exhaustion can stop a process. Docker health checks mark an unhealthy
container but do **not** restart it by themselves. Monitor failed health checks,
restart loops, disk usage, memory, provider errors and backup age externally.

## Configuration and authentication

| Variable | Meaning |
| --- | --- |
| `JARVIS_MODE` | `production` or `development`; image/Compose use production |
| `OPENAI_API_KEY` | Required nonempty provider key in production |
| `JARVIS_AUTH_TOKEN` | Required random printable token, 32–512 characters; use at least 32 random bytes encoded as hex/base64url |
| `JARVIS_PUBLIC_ORIGIN` | Exact public HTTPS origin, e.g. `https://jarvis.example.com`; HTTP permitted only on loopback for local validation |
| `JARVIS_HOST` / `JARVIS_PORT` | Server bind; image uses `0.0.0.0:8000`, direct Python defaults to `127.0.0.1:8000` |
| `JARVIS_STATE_DIR` | Production state directory, outside source; Compose fixes `/var/lib/jarvis` |
| `JARVIS_DB_PATH` | Absolute database file within state; Compose fixes `/var/lib/jarvis/jarvis.sqlite3` |
| `JARVIS_PUBLISHED_PORT` | Compose loopback port, default 8000; update the public origin if changing local access port |

Use `python -m core.deployment_server` as the server entrypoint. Development mode
preserves unauthenticated local behavior and may load `.env` when creating the
provider client. Never use development mode for remote access. Production never
loads `.env` inside Python. Compose can read an external env file on the host;
that is explicit operator configuration, not image content.

All production routes, including `/`, `/docs`, `/openapi.json`, task/memory APIs
and candidate/apply APIs, require authentication. `/health` alone is unauthenticated
and returns only `{"status":"ok"}` or a generic 503. It checks database access;
it does not call OpenAI or certify Bubblewrap availability.

Browsers use their native HTTP Basic challenge: username `jarvis`, password the
random token. This fixed username grants nothing without the secret token. API
clients may instead send `Authorization: Bearer <token>`. Tokens are compared using
constant-time digest comparison, never URLs, cookies or local storage. There are
no built-in/default credentials or persistent sessions. Browser credentials may
be cached until the browser closes; rotate the token and recreate the container
to revoke access. Do not use a memorable human password in place of a random token.

Host validation uses the configured origin. After authentication, any explicit
`Origin` must exactly equal `JARVIS_PUBLIC_ORIGIN`, even for safe navigation;
`null`, other origins, and trailing-slash variants are rejected. Requests marked
`Sec-Fetch-Site: cross-site` are rejected except for `GET`/`HEAD` to the UI root `/`
with both `Sec-Fetch-Mode: navigate` and `Sec-Fetch-Dest: document`. This permits
opening Jarvis from an external link after the Basic challenge, while cross-site
API reads, fetches, frames, and all state-changing methods remain blocked. Missing
or inconsistent navigation metadata does not qualify for the exception. The UI
root serves only the static document and does not mutate state.

Authenticated clients without Origin/Fetch Metadata headers remain supported for
CLI/API compatibility; headers are browser CSRF signals, not credentials or a
defense against non-browser clients that already possess the token. A forged
navigation tuple cannot exempt an API route, unsafe method, or mismatched Origin.
Responses use `no-store` and anti-framing headers. Forwarded headers are not trusted; Caddy preserves Host
and Authorization. Always use HTTPS remotely: Basic/Bearer credentials must not
cross an unencrypted network. Keep the backend port loopback-only and preserve
these checks when introducing another proxy. Put any additional rate limiting
or identity-aware access proxy in front; do not replace app authentication with
an unverified forwarded-user header.

Supply secrets through your existing secret-management process in an external
file readable only by the deployment operator (mode 0600), for example
`/etc/jarvis/runtime.env`. It needs the four production values: mode, API key,
auth token, and public origin. No credentials are supplied or generated by this
repository. Docker administrators can inspect runtime environment variables and
must be trusted. Do not print `docker compose config` output with real secrets;
use `config --quiet`. Keep secret files and backups outside the repository.

## Local Docker run

Install a working Docker Engine/Desktop with Compose v2; on WSL enable integration
for this distribution. Supply an existing protected runtime env file with
`JARVIS_PUBLIC_ORIGIN=http://localhost:8000`. Run from the repository:

```sh
docker compose --env-file /etc/jarvis/runtime.env config --quiet
docker compose --env-file /etc/jarvis/runtime.env build --pull
docker compose --env-file /etc/jarvis/runtime.env up -d --wait
docker compose --env-file /etc/jarvis/runtime.env ps
curl --fail http://localhost:8000/health
curl --silent --output /dev/null --write-out '%{http_code}\n' http://localhost:8000/tasks
curl --user jarvis http://localhost:8000/tasks
```

The unauthenticated `/tasks` request must return 401. `curl --user jarvis` prompts
for the token rather than putting it in shell history. Open `http://localhost:8000`
and use the browser challenge. The UI appearance remains unchanged. `up --wait`
requires a recent Compose version; otherwise inspect the health state manually.

Validate persistence by creating a conversation/memory through the UI/API, then:

```sh
docker compose --env-file /etc/jarvis/runtime.env restart jarvis
docker compose --env-file /etc/jarvis/runtime.env up -d --force-recreate --wait
```

Confirm the same IDs and data exist after both steps. `down` preserves the named
volume; **`down -v` deletes it**. Keep the same Compose project name (`jarvis`) so
recreation attaches the same volume. No application code lives in that volume.
The image alone does not enforce Compose restrictions; use this Compose file for
production rather than an unrestricted standalone `docker run` command.

## Startup, shutdown and recovery

Startup validates production configuration and non-symlinked paths, creates state
and candidate directories, probes writable storage, initializes/migrates SQLite
transactionally and runs `PRAGMA quick_check`. Failure prevents readiness. Schema
initialization remains idempotent. UID 10001 must own a pre-populated/restored volume;
new named volumes inherit the image directory ownership. Container recreation
preserves SQLite, journals/sidecars and candidate worktrees under the state volume.

SIGTERM stops accepting new traffic and gives in-flight requests up to 360 seconds
to drain; Compose allows 390 seconds before a forced kill. Lifespan closes SQLite
on shutdown or startup failure. Commits already persisted survive abrupt exits.
In-flight model work is not automatically resumed or replayed. Existing candidate
leases, interruption handling, review expiry and apply recovery rules still apply.
Single-process ownership avoids adding concurrent writers across app replicas.

## Candidate/self-edit support

In a supported Jarvis maintenance checkout, candidate test snapshots receive only
fixed deployment runtime modules and their regression test, checked byte-for-byte
against the trusted running version. This follows the existing protected-policy
dependency mechanism; candidate reads/edits of deployment controls remain denied.
Test environment clearing still excludes API/authentication secrets and state.

The production image is an immutable application, **not a writable Git checkout**.
Its intentional lack of `.git` means creating a self-edit candidate rejects at the
existing baseline gate. Applying to the running image is also unavailable. Audit
records in SQLite remain persistent. Existing HTTP and Assistant tools and exact
approval/business rules are unchanged; the deployment does not bypass them.

Bubblewrap is installed for the existing sandboxed test tool. Default Docker
seccomp, kernel user-namespace policy and AppArmor may prevent nested namespaces.
If Bubblewrap cannot start, test execution fails closed; there is no unsandboxed
fallback. Never add `--privileged`, `SYS_ADMIN`, an unconfined seccomp profile,
Docker socket access or host mounts to make it work. `/health` can be healthy
while repository tests/self-edit are unavailable. Verify isolation separately on
the target host before promising those capabilities.

Use the existing trusted development/maintenance checkout for self-edit and
human-approved apply, then rebuild/recreate the immutable service image through
your release process. A future isolated maintenance service needs its own design;
this Compose file does not provide one. Restored candidate records retain their
original source/worktree paths, manifests, hashes and audits. They are not portable
approvals: relocating files to `/var/lib/jarvis` or changing a source baseline does
not authorize application. Do not rewrite identity/path/hash fields to force an
old candidate through. Inspecting relocated worktrees can invalidate stale state
under the existing checks; retain the original environment for historical apply
recovery and create new candidates for a new checkout.

## Backup, restore and migration

The entire state volume matters: SQLite contains conversations, messages, tasks,
activity, memories, candidate events and apply journals/audits; `candidates/`
contains private repositories/worktrees. Back up both consistently. Keep backups
encrypted off-host and access-controlled; they contain personal data and code.
A copied live SQLite file alone is not a safe backup, especially with sidecars.

The simplest consistent backup pauses Jarvis. From a private backup directory
outside the source checkout, using the same Compose file/project and env file:

```sh
# Run from the repository; choose a protected absolute backup directory first.
docker compose --env-file /etc/jarvis/runtime.env stop jarvis
docker run --rm --read-only --network none --cap-drop ALL --security-opt no-new-privileges:true \
  --user 10001:10001 --mount source=jarvis_state,target=/state,readonly \
  --entrypoint tar jarvis:local -C /state -czf - . > /secure/backups/jarvis-state.tar.gz
docker compose --env-file /etc/jarvis/runtime.env start jarvis
```

Set `umask 077` before writing the archive, use timestamped names instead of
silently overwriting good backups, check command exit statuses, and test archive
integrity. Record the image digest, schema version, deployment configuration and
source baseline separately. Do not include secrets in public backup metadata.
For lower downtime use SQLite's backup API for the database and a coordinated
snapshot of worktrees; do not assume database-only backup covers candidate files.

Restore first into a **new** volume while the app is stopped:

```sh
docker volume create jarvis_restore
# Populate volume ownership from the image's pre-owned /var/lib/jarvis directory.
docker run --rm --read-only --network none --cap-drop ALL --security-opt no-new-privileges:true \
  --mount source=jarvis_restore,target=/var/lib/jarvis --entrypoint true jarvis:local
docker run --rm -i --read-only --network none --cap-drop ALL --security-opt no-new-privileges:true \
  --user 10001:10001 --mount source=jarvis_restore,target=/var/lib/jarvis \
  --entrypoint tar jarvis:local -C /var/lib/jarvis -xzf - < /secure/backups/jarvis-state.tar.gz
```

Extract only your trusted backup. Point the Compose `state` volume at
`name: jarvis_restore` with `external: true` using an operator-owned override,
then start the same compatible image, check health, authentication, record IDs,
audit records and integrity before resuming traffic. Keep the old volume for
rollback. Never downgrade across incompatible database schemas without restoring
the matching backup and image together.

Migrating existing host data requires a maintenance window: stop the old Jarvis,
back up its **entire configured state directory**, seed a new volume from that
backup, ensure UID/GID 10001 ownership, and verify record counts/IDs before switching
traffic. No migration or existing-state rewrite is performed automatically. Handle
original-path candidate recovery in the original environment as described above.

## Ubuntu VPS behind Caddy

1. Provision a supported Ubuntu VPS; install Docker Engine and Compose v2 from
   Docker's official repository and Caddy from its official package repository.
   Enable their systemd services for boot. Keep the host patched. Docker group
   membership is effectively administrative access.
2. Copy a reviewed release checkout or load a reviewed image. Provision the
   protected external runtime env file using your secret-management process.
   Set `JARVIS_PUBLIC_ORIGIN=https://jarvis.example.com`. No deployment credentials
   belong in this repository. Back up existing state before migration/upgrades.
3. Run the local build/config/health/auth/persistence checks above on the VPS.
   Keep the backend published only on 127.0.0.1. Do not open TCP 8000 in either
   the host firewall or provider firewall; account for Docker's firewall rules.
4. Point the domain's DNS records to the VPS. Permit 80/443 for Caddy and restrict
   SSH to your administration path. Install `deployment/Caddyfile.example` as a
   starting point, replacing the domain. Caddy obtains/renews HTTPS certificates;
   DNS and reachability must be correct. Validate the Caddy configuration before
   loading it. Host and Authorization headers must reach Jarvis unchanged.
5. Test externally: HTTPS health succeeds; UI and sensitive APIs without credentials
   return 401; a valid token succeeds; wrong tokens/host/cross-origin requests fail.
   Confirm port 8000 is unreachable remotely and HTTPS renewal is monitored.
6. Configure uptime/health, disk, errors and backup-age alerts; execute a restore
   drill and a host reboot test before treating the service as continuously available.

Reference semantics: [Docker Compose service controls](https://docs.docker.com/reference/compose-file/services/),
[Uvicorn shutdown/settings](https://www.uvicorn.org/settings/), and
[Caddy reverse proxy/HTTPS patterns](https://caddyserver.com/docs/caddyfile/patterns).
The application does not need to trust client-supplied forwarded headers to work
behind Caddy at a dedicated origin. URL subpath hosting is not configured here.

## Logging and release validation

The server emits one JSON object per log line to stdout/stderr. Request entries
contain method, route template, status and duration, not query strings, bodies,
Authorization headers or raw exception details. Lifecycle messages show readiness
and shutdown. Compose rotates Docker logs at 10 MiB per file, five files per
container. Use `docker compose logs --tail=100` for bounded troubleshooting and
ship logs to an access-controlled collector for retention/alerts. Do not enable
provider HTTP debug logging or configure a proxy to log credentials.

Before release run the full Python and UI suites, build the image, validate Compose
with `config --quiet`, check image history/config/files for unexpected env files
or secrets without printing their contents, then run the health/authentication and
restart/recreation checks. Image build and runtime validation require a functioning
Docker installation; source-level tests do not prove container behavior. Verify
Bubblewrap on the target host separately and accept its fail-closed limitation.
No deployment, credential provisioning, commit or push is part of these instructions.
