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
commit, repository observation state, last inspected/updated timestamps and
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

`inspect_portfolio_project(project_id)` refreshes only Git identity metadata at
the two fixed configured roots. The subprocess is `/usr/bin/git
--no-optional-locks -C <fixed-root> rev-parse --show-toplevel HEAD`, with a bounded
timeout and stripped environment/global Git configuration. Roots and Git links
must be canonical. No status/index operation, source-file inspection, credentials,
trading runtime or service action is permitted. CreatorOS and other projects
without an authorized fixed inspector return registered information only.

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
  metadata-only Trading Bot need requirements/access clarification. Unregistered
  explicit project IDs produce clarification rather than invented identities.

Known project names/IDs can infer association; ambiguous requests remain null.
For “connect Trading Bot to Jarvis”, the referenced external project is Trading
Bot. An explicit registered ID takes precedence. A concrete Jarvis-side change
should associate with `jarvis-core`; a project reference never extends access.

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
supplied baseline rather than silently replacing it. The configured Jarvis root
overrides a stale saved location. Git observations are timestamped history, not
proof of project runtime compatibility or future access.

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
