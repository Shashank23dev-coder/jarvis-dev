# Development test sandbox

The trusted development checkout prefers Bubblewrap. Before each test command it
runs a fixed harmless command with the same namespace, mount and environment
configuration as the real test. Missing binaries or failed probes select Docker
only in development. Production mode never selects Docker. A missing local image,
unreachable daemon, failed hardened-container probe, or missing effective cgroup
limits denies execution. Test failures never cause a retry on another backend.
There is no unsandboxed Python or JavaScript fallback.

Build the separate runtime from the reviewed host checkout:

```sh
./sandbox/build-runtime.sh
```

The build script sends only the runtime Dockerfile, trusted supervisor/signal guard and locked
requirements to Docker. It does not send the repository, `.env`, Git history or
state. The image contains Python 3.14, Node, Git and dependencies; it has no
application checkout, credentials, volumes or production configuration. The trusted
local tag `jarvis-sandbox:local` is resolved to its immutable image ID for each
execution and recorded in evidence. Execution never builds or pulls images. Rebuild
this image when dependencies or the guard change. Do not build it from candidates.

The trusted host launcher talks only to `/var/run/docker.sock`, using an absolute
Docker CLI path and a cleared environment/home, bypassing user Docker contexts and
configuration. The service account's daemon access is privileged trusted-host
authority; candidates never receive the socket or a Docker client capability.
Do not add Docker access to the production service or image.

Each container has exactly one bind mount: a fresh, policy-filtered snapshot at
`/workspace`, read-only and nonrecursive. Neither the source checkout, candidate
Git metadata, production state nor any host home/runtime directory is mounted.
Protected paths, symlinks, hardlinks and special files are excluded using existing
descriptor-based reads. Snapshot limits remain 10,000 files, 1 MB per file and
50 MB total. Known secret/state/Docker-config paths are additionally denied by the
shared path policy. Runtime-only protected dependency injection remains limited to
Jarvis's trusted policy and deployment dependencies and retains byte comparisons
for candidates. Arbitrary secrets disguised as ordinary source are not detectable
by filename policy; only reviewed source belongs in a candidate repository.

Containers run as UID/GID 10001, with a read-only root, all capabilities dropped,
no-new-privileges, Docker's default seccomp/AppArmor confinement, no network, no
shared IPC, and a private cgroup namespace. They have no restart policy, health
check or persistent logs. Resource settings follow Docker's documented
[container runtime controls](https://docs.docker.com/engine/containers/run/):

| Resource | Docker limit |
| --- | --- |
| CPU | 1 CPU quota |
| Memory | 512 MiB, no extra swap |
| Processes/threads | 128 |
| Writable scratch | 256 MiB `/tmp`, noexec/nosuid/nodev |
| File descriptors | 256 |
| Core dumps | disabled |
| Wall clock | 120 seconds by default, capped by remaining candidate budget |
| Combined stdout/stderr | existing 1 MB executor limit |

The probe checks the actual memory, swap, CPU and PID cgroup limits, non-root UID,
no-new-privileges and seccomp. Cgroup v2 is required for Docker fallback. Scratch
and ordinary container device pseudo-files are the only writable areas; tests
cannot modify any candidate/source path. Approved edits still happen through the
trusted CandidateEditor with unchanged file/line/tool/iteration budgets.

The host bounds output and time and forcibly removes the uniquely named container
in `finally`, including on truncation. Cleanup has an independent 10-second budget
so an exhausted candidate deadline cannot skip it. Cleanup failure denies success.
Inside the container, a trusted non-dumpable Python supervisor is PID 1. Its
deadline and exit tear down descendants even if the host client dies. Disabling
dumpability prevents same-UID candidates opening the supervisor process memory.
Before executing tests, the trusted guard installs an inherited additional seccomp
filter preventing SIGSTOP to PID 1 (and pidfd SIGSTOP), plus process-memory/ptrace
writes. Ordinary child-process signals remain available for test supervision.
Candidates cannot suspend the timer; killing PID 1 simply ends the container.
Docker `--rm` removes it at exit. A daemon or host failure can still require
operator cleanup of containers labelled `jarvis.sandbox=true`.

Both backends return the same CommandResult/ProcessResult schema. The additive
`sandbox` object records backend, UTC start/end, configured limits, resolved image,
selection reasons, exit status, timeout and truncation. Candidate test records and
post-apply verification persist this object beside the original stdout/stderr.
Existing fingerprint, exact patch, review, approval and post-apply checks are
unchanged. Non-execution tools and trusted test adapters use an empty audit object.
The original Bubblewrap backend retains its existing resource behavior (wall and
output bounds; CPU/memory/PID/scratch quotas are reported as null).

Validation commands from the trusted host checkout:

```sh
umask 022
JARVIS_MODE=development JARVIS_DB_PATH=:memory: OPENAI_API_KEY=test-provider-key \
  JARVIS_DOCKER_TESTS=1 .venv/bin/python -m pytest -q -ra
node --test tests/ui.test.cjs
```

Live checks force Docker and exercise secrets, read-only mounts, no network,
effective cgroups, OOM/PID enforcement, timeout, truncation, timer tampering/client
death, candidate tests and post-apply verification. Test applies and commits occur
only in temporary fixture repositories. The live Docker flag is not forwarded to
candidate containers. Nested sandbox execution is unavailable there by design.

Limitations: containers share the host kernel and trust Docker, its runtime, the
reviewed dependency image and host launcher. This is not a VM or a hostile-host
boundary. Python in the image is 3.14 whereas this VPS's host venv is 3.12; tests
must be checked on their intended runtime. Concurrently malicious host processes,
disguised secrets in approved source and aggregate concurrent-job quotas require
separate controls. No sysctl/AppArmor changes, production deployment or service
restart are needed or performed by this backend.
