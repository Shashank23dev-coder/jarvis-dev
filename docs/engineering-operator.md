# Jarvis Engineering Operator v1

The operator coordinates existing engineering jobs, candidates and providers.
It runs in the existing VPS engineering worker, with durable checkpoints in the
same private SQLite database. It does not create another daemon, authority
system, provider credential store or deployment endpoint.

## Submission and modes

In an authenticated owner conversation, inspect the registered project and use
`start_engineering_operator`. The conversation's goal is the job goal. Example
tool arguments (paths must exist in the selected project's inspected catalog):

```json
{
  "project_id": "jarvis-core",
  "inspect_paths": ["core/example.py"],
  "targeted_tests": ["tests/test_example.py"],
  "problem_detected": "Describe the observed defect.",
  "hypothesis": "Describe the proposed correction.",
  "execution_mode": "CODEX_BUILD_CLAUDE_REVIEW"
}
```

Omitting `execution_mode` on this tool selects `CODEX_BUILD_CLAUDE_REVIEW`.
`create_engineering_job` also accepts an explicit mode. Jobs without a mode
retain their existing workflow. Unknown or ambiguous projects need clarification;
the model cannot supply a repository root or register a project.

| Mode | Implementation | Independent review |
| --- | --- | --- |
| `CODEX_ONLY` | Codex | Existing OpenAI reviewer |
| `CLAUDE_ONLY` | Claude Code | Existing OpenAI reviewer |
| `CODEX_BUILD_CLAUDE_REVIEW` | Codex | Claude Code |
| `CLAUDE_EXTERNAL_REVIEW` | Claude Code | None automated: external review by grok |

`CLAUDE_EXTERNAL_REVIEW` is an owner server setting, not a model choice. It is
usable only while the server process environment has
`JARVIS_ALLOW_CLAUDE_EXTERNAL_REVIEW` set to exactly `1` or `true`; it is off by
default. While it is off, creating a job in this mode (chat tool, broker or
`EngineeringJobs.create`) is refused with `ToolDenied`. The worker checks it
again before running or reviewing, and apply checks it again, so a job created
while the setting was on cannot continue after it is turned off. No tool
argument, prompt text or HTTP field turns it on.

Claude Code builds exactly as in `CLAUDE_ONLY`. No provider (Codex, OpenAI or
Claude) reviews the candidate; after passing tests the tested bytes are sealed
with `review_result.verdict = external_review_required` and `reviewer = grok`.
That sealed status is never a pass. The job ends in `waiting_for_approval` after
the full release preparation (never `completed`) and waits for grok's review and
owner approval.

grok's verdict is recorded on the host only, with a CLI that no HTTP route, tool
registry or chat tool can reach:

    python -m core.external_review record-review <candidate_id> \
        --sha <candidate fingerprint> --verdict passed|needs_revision --reviewer grok

It refuses candidates outside this mode and any `--sha` that is not both the
candidate's current fingerprint and its sealed reviewed fingerprint, then stores
`external_review = {verdict, reviewer, sha, recorded_at}` on the candidate.
Candidate apply for this mode requires that record with `verdict = passed` and
`sha` equal to the reviewed fingerprint, plus the usual fingerprint and
diff-hash checks. A missing review, `needs_revision` or a different sha is
refused ("Review evidence is incomplete or changed"). Resealing the candidate
discards an earlier record. All other modes keep their provider review gate
(`reviewer_result.verdict = passed`) and `reviewer_must_differ_from_author`.

The single-agent modes name the engineering implementation agent; they retain
the existing independent OpenAI review gate. Routine implementation/debugging
uses Codex. The combined mode is the default for independent Claude review.
Provider selection is explicit: a selected provider's quota/authentication/
availability failure blocks that step. There is no silent substitution. Existing
legacy jobs retain the registry's fallback behavior.

## Execution

1. Resolve the trusted project root and inspect repository, project, provider
   observations and available runtime evidence. Unknown runtime stays unknown.
2. Create a private bare clone and branch/worktree using the existing candidate
   allocator. No shared object hardlinks, source hooks or live checkout edits.
3. Record baseline tests, then request a structured implementation proposal.
4. Apply validated edits through the existing host editor and run focused tests.
5. Request independent review bound to the tested bytes. `needs_revision` must
   contain concrete `path`, `problem`, `requested_change` findings. Only these
   findings, the goal and necessary source/test context return to the builder;
   reviewer narrative and dialogue do not. Validate and review the revision.
6. After `passed`, commit the exact reviewed bytes on the private branch. For
   Jarvis, call the existing exact-commit release validator against this commit,
   with a private preparation journal. Its complete Python/UI results become
   fingerprint-bound candidate test evidence. Other registered projects retain
   the existing full candidate test path before review.
7. Report candidate preparation and stop at the existing promotion/approval
   path. Private preparation always has `controller_validated=false` and never
   produces a deployment approval phrase.

Codex uses the existing API adapter in a subprocess. Claude uses the existing
tool-disabled CLI subprocess adapter. Providers return data; the host owns all
edits, Git operations and sandboxed tests. Two agents never edit a checkout
concurrently. The existing inherited worker flock serializes jobs and remains
held by execution children if their supervisor exits.

## Persistence and recovery

Existing job and candidate records retain the job ID, project, goal, source
identity, branch, worktree, provider attribution, test output and review result.
The operator adds phase, revision count, step start/end times, provider/model
metadata, response hashes, observations, candidate commit and preparation result.
Codex/OpenAI model IDs come from existing configuration. Claude's adapter does
not report its resolved model; its model is explicitly null/unreported.

Phases are `inspect`, `baseline`, `implement`, `apply`, `validate`, `review`,
`commit`, `readiness`, `done`. Each step records start and completion. Saved
proposals can recover after partially applied edits only when the worktree
contains known old/new bytes. A committed but uncheckpointed commit must have
the expected parent, branch and clean tree. Unexpected edits invalidate evidence
and mark execution as operator-assisted.

`inspect_engineering_job`, `list_engineering_jobs` and
`inspect_engineering_runtime` work across conversations. The existing systemd
worker continues after SSH disconnection. After actual process interruption,
expired leases permit recovery under the same flock. Successful proposals,
tests, review and commits are reused. A provider call that never persisted a
valid result may need another invocation; remote exactly-once billing cannot be
guaranteed. Release preparation reuses fresh evidence from its private journal.

Quota and availability failures retain the same candidate and worktree. Use
`resume_engineering_job` after resolving the blocker; provider observations have
the existing 300-second backoff. Retries retain history and follow the existing
three-attempt limit and owner-controlled additional attempt mechanism. Changing
checkpointed inputs or execution mode requires a new job. An interrupted partial
Git setup that cannot be safely identified blocks for inspection.

Owner-visible responses are fixed, concise checkpoint, blocker, review or
preparation statements. Full paths, proposals and logs stay in private evidence.
Provider adapters and detected external edits retain the existing honest
autonomous/operator-assisted attribution rules.

## Authority and limits

Routine goal submission, implementation, tests, review and private preparation
need no new approval phrase. Authentication remains required. The existing
authority classifier governs apply, destructive actions, financial commitments
and permission expansion. This coordinator cannot raise limits, configure
providers, purchase capacity, grant permissions or deploy production.

An attempt retains the existing candidate limits: two implementation proposals,
four provider turns, eight files, 400 changed lines, 64 internal operations and
900 seconds (or stricter host limits). Jarvis release preparation has a separate
2,430-second subprocess deadline for the existing four 600-second validation
batches, with a 2,460-second job lease. No monetary budget or purchasing API is
introduced; existing provisioned-provider and financial-escalation rules apply.

Jarvis candidate integration, promotion to the configured development checkout,
fresh controller validation and production approval remain separate existing
operations. Even an approval phrase embedded in an engineering goal cannot make
this coordinator deploy. The deployment controller still requires the owner's
exact phrase for its exact eligible SHA in the current deployment request.

## Validation and activation

Acceptance tests use real Git worktrees, SQLite, worker processes and locks with
scripted provider/test/release adapters. They cover all three modes, revision
handoff, quota and process recovery, checkpoint replay, worktree fencing,
readiness invalidation, owner authentication, concise projection and no deploy.
Existing release tests cover exact approval and controller-owned evidence.
These tests do not prove current provider quota or run paid live acceptance.

The existing candidate path/test restrictions still apply to registered projects;
v1 is not a general shell agent or a new language build system. Full release
validation can fail on environment/inventory issues and leaves the PASS and
commit available for diagnosis/retry. Prepared evidence is historical; promotion
and deployment recheck their existing freshness and identity gates.

Review and integrate this change through the existing approved development
promotion process before restarting the broker/worker to load it. Exposing the
new tool in the production chat runtime additionally needs the existing approved
release process. Implementation alone does not activate or deploy it.
