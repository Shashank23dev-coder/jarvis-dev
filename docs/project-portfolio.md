# Project portfolio and capability registry

Jarvis Executive owns projects. Providers, tools and future managed agents are
resources, not project owners. This change adds three small SQLite JSON tables
alongside existing jobs/provider state, with no database version change or new
framework. `Portfolio` registers projects/capabilities, `Decisions` records a
conservative pre-engineering policy, and the existing owner tools/broker, queue
and self-state consume their closed projections. Existing provider routing,
candidate isolation, apply and deployment authority are unchanged.

## Records and persistence

`portfolio_projects` stores `project_id`, name, description, category, status,
repository (nullable means unknown), runtime notes, executive (`Jarvis` only),
dependencies, known capabilities, limitations, read-only flag, baseline/current
commit, repository observation state, optional `repository_inspection` (available,
reason, canonical root, observed revision, baseline conflict, timestamp), last inspected/updated timestamps and
typed evidence source/scope/time. Registration is host-only; seeding uses insert
if absent and preserves existing records. Explicit trusted replacement updates a
record. A registered repository never becomes an executable root argument.

Initial projects:

| ID | Location and status | Evidence / limitations |
| --- | --- | --- |
| `jarvis-core` | `/home/ubuntu/jarvis-dev`, active | Jarvis's configured development source. Production is separate. |
| `trading-bot` | `/srv/jarvis/projects/trading-bot`, unproven runtime, read-only | Independent Git repository, supplied baseline `e03c307c64fdaaf267ecb7be3898325af555f966`. Windows origin; Linux runtime unproven and live Windows runtime external. |
| `creator-os` | Unknown location, `needs_discovery` | Conceptual identity only. No guessed path or runtime. |

`portfolio_capabilities` stores `capability_id`, name, category, system, native /
external / managed kind, registration, nullable configured state, current
usability, task types, optional project scope, limitations, nullable cost/latency
notes, timestamped evidence, last success/failure and freshness interval.
States are available, unavailable, unproven, unknown, not_authenticated,
quota_blocked and invocation_failed. Registration/configuration alone never
proves execution. Evidence normally expires after 300 seconds; unknown is not
absence. Provider evidence retains the existing provider registry's tighter
meaning, authentication scope, timestamps and histories.

Initial implemented capabilities:

| ID | System |
| --- | --- |
| `codex-engineering` | Existing Codex engineering provider |
| `claude-code-engineering` | Existing Claude Code adapter; installation does not prove authentication or remote usability |
| `jarvisbrain-reasoning` | JarvisBrain reasoning, including the existing OpenAI route; current evidence names the actual provider |
| `engineering-jobs` | Existing persistent engineering queue |
| `docker-sandbox` | Existing hardened development Docker sandbox |
| `repo-files` | Existing filtered repository/file tools |
| `testing` | Existing isolated testing |
| `independent-review` | Existing provider-independent candidate review |
| `candidate-workflow` | Existing controlled candidate implementation workflow |
| `candidate-apply` | Existing human-approved candidate apply |
| `deployment-pipeline` | Existing controlled deployment pipeline |
| `memory` | Existing persistent SQLite memory/experience |
| `self-state` | Existing runtime capability awareness |
| `provider-routing` | Existing engineering provider selection/fallback |

Categories also allow research, external integrations, browser/web and managed
agents, but no nonexistent implementations are seeded. Unknown prices/latencies
remain null. Native runtime usability starts unproven; actual completed task
tool/Brain audit records can establish scoped, timestamped positive evidence.
Sandbox executable presence and past deployment status do not prove current
execution. This change never probes a paid provider or authenticates Claude.

## Owner tools and pre-engineering policy

`inspect_engineering_runtime` now returns `portfolio` and up to five recent
`decisions`, as well as existing heartbeat, jobs and provider registry/routing.
It does not invoke providers or project runtimes.

`inspect_portfolio_project(project_id)` refreshes Git identity metadata through
the same resolver as `inspect_project_repository`. The latter accepts a project
ID and a bounded read operation: inspect, list_files, read_text, literal search_text,
filtered git_status or git_diff. Trusted host Portfolio metadata supplies the
canonical registered repository root; callers cannot supply roots. The adapter
uses existing `RepositoryTools` path policy, descriptor-based no-follow reads,
bounded output, stripped subprocess environment and safe Git arguments. Identity
checks use fixed `rev-parse --show-toplevel HEAD` through that native executor.

For requests naming a known project, existing list/read/status/diff tools and
the read-only command facade receive the resolved project context in a per-task
registry copy. Production chat uses the existing owner broker/mailbox and its
host database; the chat container's filesystem and prompt text do not supply
roots. Tool definitions include the resolved project ID. No new transport or
permission system is introduced. Default repository tools retain their existing
behavior when no project is referenced. Automatic association recognizes the
three initial identities; other registered IDs can be explicitly inspected.

Unknown location, missing/inaccessible/unsafe roots, unavailable Git identity and
baseline conflicts return structured evidence without falling back to Jarvis.
CreatorOS remains inaccessible until trusted host registration establishes its
root. A baseline mismatch reports the actual revision and blocks source reads.
Protected paths, traversal, root/parent/file symlinks and hardlinked files are
rejected or omitted. Text passes existing credential redaction with additional
quoted-key redaction. No external project writes, candidates, tests, arbitrary
commands, runtime/service operations or deployment are added.

`decide_engineering(project_id=null, required_capabilities=[])` evaluates the
host-bound current request against the portfolio and live provider/task evidence.
It persists a digest of the goal, optional task/project reference, outcome,
fixed policy rationale, relevant and explicitly required capabilities, observed
states, missing prerequisites, appropriate resource IDs, timestamp, clarification
flag and the fixed configured-dev execution scope. Raw goals, prompts, provider
errors and credential material are not copied into decision records.

The policy is deliberately small and conservative:

- `USE_EXISTING`: recognized requests to add/use Codex or Claude support reuse
  existing adapters/routing. Authentication and quota remain separate blockers.
  Project inspection/assessment requests require fresh scoped repo-files evidence;
  once confirmed, they reuse the bounded repository capability.
  Other explicitly requested capabilities require fresh usable evidence and
  matching project scope before a reuse decision.
- `INTEGRATE`: a known project connection should use current Jarvis-side tools,
  jobs and provider routing to define an integration boundary. Process/SSH access
  and external runtime access are not assumed. A broad connection request needs
  the desired interaction and runtime details before implementation; it does not
  queue a speculative protocol.
- `BUILD`: a specific engineering change or requested absent capability justifies
  the existing controlled candidate workflow. Actual provider selection and
  execution blockers remain the existing router's responsibility.
- `NEED_MORE_INFORMATION`: ambiguous goals, unlocated projects, unknown project
  IDs, unavailable/unproven required operations or requests to change the
  read-only Trading Bot need requirements/access clarification. Unregistered
  explicit project IDs produce clarification rather than invented identities.

Known project names/IDs can infer association; ambiguous requests remain null.
For “connect Trading Bot to Jarvis”, the referenced external project is Trading
Bot. An explicit registered ID takes precedence. A concrete Jarvis-side change
should associate with `jarvis-core`; a project reference never extends execution authority.

## Engineering jobs and runtime truth

New job submissions and the legacy owner candidate path run preflight even if a
caller omits the decision tool. A prior explicit decision's required capabilities
are retained when refreshing the submission decision, preventing a queue call
from bypassing missing information. Only a BUILD decision without unresolved
clarification proceeds. Reuse/integration/clarification rationales remain
persisted even when submission is refused. Existing task-bound idempotency
returns the originally committed job/decision.

`create_engineering_job` accepts optional `project_id`; jobs persist the resolved
ID and decision, and candidates retain project/decision IDs in source context.
The worker strips the project argument before invoking the existing service and
always executes in its host-configured dev checkout. Old records with neither
field are projected as null and execute normally. Recovery of inspection inputs
cannot reassign a job to another project. No destructive migration is required.
Provider/operator attribution is untouched.

Self-state exposes portfolio, capability usability, job/project association and
recent rationale. Owner-only observations remain owner-only; an unavailable
broker gives unknown portfolio state. Runtime provider observations override
stale capability claims, completed tool/Brain evidence is scoped and expires,
and contradictions are surfaced. A changed Git HEAD is reported alongside the
supplied baseline rather than silently replacing it. Owner runtime snapshots
refresh each registered repository's bounded Git/access evidence and derive
repo-files usability/project scope from fresh observations. Stored paths do not
override failed live access. Git observations are timestamped history, not proof
of project runtime compatibility or future access.

## Limits and operations

Registration APIs are for trusted host code, not model-controlled writes.
Automatic association recognizes the three seeded identities; other project IDs
can be explicit. The conservative rules do not constitute general semantic
planning, discovery, external access, MCP, managed agents or a new authorization
system. Broad integrations require a clarified Jarvis-side task. Registry reads
do not run Docker, authenticate providers, touch production or deploy anything.
The existing private development database receives seeds on first registry load;
this implementation does not restart services. Back up it with existing job and
candidate state. Tests use temporary SQLite databases/repositories and scripted
providers; live credentials and project runtimes are unnecessary.

## Validation checkpoints

Implemented from `42aa7cb5d2ebd67aeb50a5746d6c428911186173`:

- `3b5dcdb`: persistent portfolio, capability records and preflight policy;
  initial focused tests: 20 passed.
- `43b883c`: owner tools/broker, job association, legacy-path preflight,
  self-state, compatibility/safety coverage and documentation.

Validation on the integrated implementation:

- `.venv/bin/python -m pytest -q tests/test_engineering_portfolio.py
  tests/test_portfolio_integration.py tests/test_engineering_jobs.py
  tests/test_engineering_action.py tests/test_self_state.py
  tests/test_candidate_bridge.py tests/test_candidate_tools.py`:
  **197 passed, 1 skipped**.
- `.venv/bin/python -m pytest -q`: **1081 passed, 14 skipped** in 626.90 seconds.
  Skipped checks retain existing opt-in Docker / unavailable isolation conditions.
  Existing injected test-adapter multiprocessing emitted 120 fork deprecation warnings.
- `node --test tests/ui.test.cjs`: **14 passed**.

Read-only Git identity checks confirmed Trading Bot's supplied baseline. No
Trading Bot source/runtime/credential/service operation, Claude authentication,
production modification, deployment or service restart was performed. Protected
release fixture hashes and the completed provider execution layer are unchanged.

## Registered repository integration checkpoint

Implemented from `2f00db807cb29d812d59cb2b63d540d144ad8d3f` to close the gap
between Portfolio metadata and the UI's fixed Jarvis repository scope. The small
adapter lives in `core/engineering_repository.py`; it resolves trusted project
IDs and delegates to existing tools and the existing host broker.

Dev acceptance used the UI's `POST /task` endpoint via FastAPI TestClient with a
scripted model and real filesystem/Git reads, a temporary SQLite database and
synthetic owner authentication. This proves the UI/tool integration, not autonomous
model planning or production deployment. Three successful read-only calls listed
181 visible Trading Bot files, read 15,149 characters from `README.md`, and checked
Git identity. Root: `/srv/jarvis/projects/trading-bot`; observed revision:
`e03c307c64fdaaf267ecb7be3898325af555f966`, matching the stored baseline. Git
status was empty before and after. No full assessment, runtime, credential,
candidate or deployment operation was performed.

Validation on the committed integration checkpoint `6b1d7d7`:

- `.venv/bin/python -m pytest -q tests/test_project_repository.py
  tests/test_engineering_portfolio.py tests/test_portfolio_integration.py
  tests/test_candidate_bridge.py tests/test_candidate_tools.py
  tests/test_self_state.py tests/test_engineering_jobs.py
  tests/test_engineering_action.py tests/test_agent.py tests/test_brain.py`:
  **315 passed, 1 skipped** (including 38 registered-repository tests).
- `.venv/bin/python -m pytest -q`: **1120 passed, 14 skipped**, 771.43 seconds;
  89 existing multiprocessing fork deprecation warnings. Optional isolation
  checks retain their existing skips.
- `node --test tests/ui.test.cjs`: **14 passed**.
- `git diff --check` and the protected release fixture hashes passed.

An existing Brain context-comparison test now fixes its retrieval clock so
wall-clock score decay cannot cause false mismatches between sequential routes.
Runtime retrieval and the completed engineering loop were not changed.

The acceptance check was repeated after the implementation commit with identical
results. Its sanitized proof is `/tmp/jarvis-project-readonly-erlxihh0/acceptance.json`.
Trading Bot Git status remained empty after the full validation run as well.
No production modification, deployment, service operation or provider
authentication was performed.
