# Controlled development-to-production releases

The trusted host controller runs from `~/jarvis-dev`. Production remains an
immutable image without Git metadata or Docker access. The old `~/jarvis/.env`
remains the secret source; the existing `jarvis_state` volume remains the state
source. Caddy continues to proxy to `127.0.0.1:8000`. No archive/SCP, checkout copy,
Compose rebuild, remote push, host package change, or Caddy reload is involved.

The controller is disabled unless `JARVIS_MODE=development` and
`JARVIS_DEPLOY_CONFIG` names a private, operator-owned configuration file. Production
mode always refuses it. Keep the development API loopback-only and accessible only
to the trusted operator: enabling this controller gives that process privileged
Docker authority. Do not mount its configuration, journal, host checkout or Docker
socket into production or candidate containers. The host operator and daemon are
trusted; application/model tool inputs cannot select paths, targets or commands.

## Release and approval contract

Only a full, lowercase 40-character Git commit SHA equal to clean current `HEAD`
is eligible. Staged changes, tracked modifications and nonignored untracked files
reject deployment. Ignored local files never enter the build: the controller reads
regular Git blobs directly through an allowlist, excluding hidden files, SSH keys,
credential names, databases, state and private-key material. Links/submodules in
the build allowlist are rejected. No Git archive attribute/filter or working-tree
bytes supply the context. Ordinary source still needs human secret/security review;
filename/content checks cannot recognize every arbitrary embedded credential.

`validate SHA` exports that commit and runs the full Python and UI suites through
the existing Bubblewrap/Docker development test sandbox. It records exit status,
backend, limits and timestamp, not potentially sensitive test output. A rerun
invalidates prior evidence before starting. Both suites must pass within 24 hours.
Validation is a trusted CLI operation, not a model tool and not a production action.

Preview reports SHA, commit message, changed paths from the current image revision
when that revision exists locally, validation/context-policy evidence, image tag
and resolved local image ID when available. The initial manually built image may
have no revision label: the preview explicitly reports an unknown baseline and
`changed_files: null`; it never invents a baseline. Source review remains a human
responsibility; the exact approval records that review in the release audit.

The entire current user message must be exactly:

```text
Approve and deploy commit <FULL_SHA> to production
```

Replace `<FULL_SHA>` with the actual SHA. Case changes, abbreviated SHAs, punctuation,
extra text, quotes, past conversation approvals and “yes/deploy it” do not qualify.
The model supplies only the SHA argument; the host supplies the original current
message to the service. Every service invocation checks the binding again. An
advance of HEAD invalidates the old approval. Dirty HEAD also rejects it.

## Build, verification and switch

1. Acquire the nonblocking `flock` on the permanent journal lock inode. An atomic,
   fsynced JSON journal stores evidence, intent, transitions and terminal results.
   Configure exactly one journal per target and use this controller exclusively;
   external Docker/Compose operators are outside the concurrency guarantee.
2. Revalidate clean HEAD and fresh evidence. Build `jarvis-release:<SHA>` from the
   exported context, with `org.opencontainers.image.revision=<SHA>`. Require the
   committed Dockerfile to match the reviewed controller's Dockerfile. Resolve and
   record the immutable `sha256:` image ID; all runs use that ID, never a mutable
   tag. This is a local image ID, not a claimed registry manifest digest. No push
   occurs. Dependencies/base packages may change between builds; byte-for-byte
   reproducible builds are outside this milestone.
3. Inspect the existing container and reject security, mount, environment or
   loopback-binding drift. Verify both its direct endpoint and public HTTPS:
   `/health`, unauthenticated `/conversations` returning 401, and authenticated
   `/conversations` returning the expected application shape. Store only hashes
   of conversation IDs for persistence comparison, never records or credentials.
4. Reject a change to `core/database.py` relative to the running image. Start the
   new image on **127.0.0.1:18000**, using a disposable, separately named state
   volume; perform health and authenticated application checks. It cannot write
   production state. Remove only this release's labelled preflight resources.
   Port 18000 must be free; an occupied port fails before production is stopped.
5. Recheck source and production identity. Persist switch intent. Add a retention
   tag for the old image, stop/drain the old container (390-second grace), rename
   it to `jarvis-previous-<release-id>`, and create the new container at the original
   name with **127.0.0.1:8000:8000** and the same named volume. There is a brief
   maintenance interval; this single-process SQLite deployment is not zero downtime.
6. Verify direct and public health, authentication and persisted conversation
   identity again. Record `deployed` only after these checks succeed. Repeating an
   approved already-deployed SHA verifies it and returns its existing record,
   without rebuilding or switching again.

Every new container uses UID/GID 10001, read-only root, all capabilities dropped,
no-new-privileges, default seccomp/AppArmor, 1 GiB memory/no additional swap, 2 CPUs,
256 PIDs, bounded 256 MiB tmpfs, init, bounded logs and `unless-stopped` restart.
It has exactly one persistent volume and no host or Docker socket bind mounts.
The existing three secret/environment values are read privately from the production
env file and passed via a mode-0600 temporary file, removed after container creation.
No values are placed in argv, build arguments, audit records or diagnostic output.
Each host Docker invocation receives only the fixed `PATH` and a fresh, empty,
mode-0700 temporary `HOME` for Docker's home-relative build metadata/config.
The directory is outside the build context, is never mounted into a container,
and is removed when the command exits, fails, times out or is interrupted.
No operator home, Docker config, SSH keys or inherited environment is supplied.
Existing production env values must match; rotate credentials separately. Public
checks disable inherited HTTP proxies and redirects; TLS verification stays enabled.

Docker behavior references: [run controls](https://docs.docker.com/engine/containers/run/)
and [persistent volumes](https://docs.docker.com/engine/storage/volumes/).

## Rollback and recovery

Any startup/switch/post-switch verification failure removes only the new release's
labelled container, renames the retained previous container back, and starts it.
Its exact image, environment and state-volume attachment are retained. Rollback
never deletes, restores over or replaces production state. It verifies security,
direct/public health, authentication and previously recorded conversation IDs.
Only a verified restore earns `rolled_back`; otherwise `rollback_failed` blocks
further deployment and requires operator reconciliation. The prior image and
container are retained after success too; no automatic image/container pruning.

Failure before switching leaves the old production container running. A process
or host crash may prevent immediate rollback: the persisted intent remains active.
Inspection is strictly read-only. Reissue exact approval for the interrupted SHA
to recover it under the same lock; that call returns the recovery result and does
not continue into a fresh deployment. Approval for another SHA is rejected until
recovery completes. A lost journal or missing retained container requires manual
reconciliation, not guesses. No background recovery daemon is installed.

This first pipeline supports backward-compatible application changes only. The
database-module identity gate does not prove arbitrary application code cannot
damage data; review all state-writing changes. It does not roll back schema/data
migrations or undo user writes. Keep the established independently tested backup
procedure. Incompatible migrations require a separately reviewed maintenance and
restore plan. Authenticated read smoke tests do not call OpenAI or prove every app
workflow. Automated fixture tests do not substitute for the first controlled VPS
release rehearsal and observation.

## Interfaces

These APIs are on the configured **development** service:

| Method/path | Purpose |
| --- | --- |
| `GET /deployments/preview/{sha}` | Read eligibility, evidence and approval syntax |
| `GET /deployments` | Latest 50 deployment/rollback audit records |
| `GET /deployments/{deployment_id}` | Read one durable result, no recovery |
| `POST /deployments/{sha}/deploy` | Body `{"confirmation":"Approve and deploy commit <SHA> to production"}` |

Normal agent tools are `preview_deployment`, `inspect_deployment`, and
`deployment_history`. `approve_and_deploy_commit` is exposed only for exact current
message approval and accepts only its bound commit. Validation/build configuration
and arbitrary rollback commands are not model arguments. Deploy/rollback outcomes
override invented model success prose with the recorded result.

The journal stores release ID, requested SHA/message, exact approval, human review
attestation, test evidence, changed files, previous container/image/revision, new
image tag/ID, timestamps, phase results, final status and rollback verification.
Errors are fixed redacted messages, not Docker/HTTP/test exception text. Keep the
journal private and backed up; it is operator-controlled audit storage, not a
tamper-proof ledger against the host administrator.

## Later controlled live deployment (not performed during implementation)

1. Review and commit this implementation, run the complete host Python/UI suites,
   and ensure `git status --short` is empty. Review the complete desired release,
   including changes since the known production baseline. Confirm existing backups
   and accept the short maintenance interval. Do not run the old Compose `up` flow
   in parallel with this controller.
2. Inspect only non-secret target metadata to confirm container `jarvis-jarvis-1`,
   named volume `jarvis_state`, and loopback port 8000. If the actual volume differs,
   edit the example configuration to that existing name; never create replacement
   production state to get past a check. Confirm `~/jarvis/.env` is mode 0600.
3. Install the example as a **private operator-owned file outside the checkout**:

   ```sh
   cd /home/ubuntu/jarvis-dev
   install -d -m 700 /home/ubuntu/.config/jarvis
   install -m 600 deployment/release-config.example.json /home/ubuntu/.config/jarvis/releases.json
   export JARVIS_MODE=development
   export JARVIS_DEPLOY_CONFIG=/home/ubuntu/.config/jarvis/releases.json
   git rev-parse HEAD
   ```

4. Substitute that full SHA in these commands:

   ```sh
   .venv/bin/python -m core.deployment_release validate <FULL_SHA>
   .venv/bin/python -m core.deployment_release preview <FULL_SHA>
   ```

   Confirm passing fresh tests and review evidence, target identity and changed
   files/unknown-baseline disclosure. Preview does not build, stop or start anything.
5. Only after the human has reviewed that exact SHA:

   ```sh
   .venv/bin/python -m core.deployment_release deploy <FULL_SHA>
   ```

   At `Exact approval:`, enter the exact sentence shown above. The command then
   builds, preflights, switches and verifies automatically, or verifies rollback.
   Keep the controller alive for the entire operation. Never print the production
   env file, full container inspection, or interpolated Compose configuration.
6. Inspect the returned record and public UI:

   ```sh
   .venv/bin/python -m core.deployment_release history
   .venv/bin/python -m core.deployment_release inspect <RELEASE_ID>
   ```

   Require `deployed` or a verified `rolled_back` result; investigate
   `rollback_failed` immediately. Retain old images/containers, journal and backups.
   Nothing in this runbook pushes Git, updates Caddy, or opens port 8000 publicly.
