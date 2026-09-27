# Evidence-grounded task experiences

Jarvis learns operational lessons through retrieval, without changing model weights
or making extra model calls. Reflections are deterministic reductions of validated
tool envelopes and host-recorded candidate test/review/apply outcomes. Model final
answers, raw outputs, file contents, patches, review prose and reasoning are never
reflection inputs.

## Storage and creation

SQLite schema version 3 adds `experiences` and a task-type/timestamp index. The
migration is transactional and repeatable; all existing tables and records remain
unchanged. Each row has a UUID, a unique source key, optional task/candidate foreign
keys, task type, outcome, UTC timestamp and JSON capped at 8,000 ASCII bytes.
The JSON contains the goal/context, attempted approach, outcome, what worked and
failed, structured evidence, reusable lesson and four-part reflection. Goals are
screened before truncating to 240 characters; other prose fields are at most 500
characters. Reflection fields are generated from evidence, not model guesses.

The agent captures a reflection after completing, failing or reaching its execution
limit for an explicit engineering, testing, inspection or candidate operation.
Intent recognition is deliberately conservative and English/text-based: requests
starting with run tests, fix, debug, implement, add, build, configure, inspect,
review and similar verbs qualify. Casual chat and conversation-recall requests do
not create experiences or search this store. A no-tool engineering answer is marked
`unverified`; read-only inspection is `observed`, never proof of a fix.

Up to 32 tool envelopes are reduced immediately; the last eight metadata summaries
are retained with total/omitted counts. A test exit code, timeout or truncation can
veto success regardless of the assistant's response. A later passing check clears
a previous failure only for the same tool and SHA-256 argument-scope identity;
arguments themselves are not stored. A different passing test cannot erase a
failure. Successful checks establish only those checks, not the entire goal.
Recovery records do not infer what caused the recovery.

Candidate reviews and final apply/rollback events also capture phase-specific
experiences. Passed review requires passing test phases bound to the reviewed
fingerprint; applied success requires post-apply verification. Passing tests never
override a negative review. Rollback is a failed apply with successful restoration,
not a successful application. Repeated terminal events update one row per task or
candidate phase. This is a compact lesson store, not the authoritative audit trail.
Candidate outcomes use all eligible phase results before retaining the last six
summaries with total/omitted counts, so the storage bound cannot hide a failed gate.
Read-only candidate preview/inspection creates no candidate reflection.

Reflection runs after the source state commits and is best-effort. Failure emits a
generic log event and cannot change task status, candidate review, approval gates,
commit or rollback. There is no background backfill/retry; an interrupted capture
may leave no lesson. Existing tasks are not retroactively mined.

## Retrieval and precedence

Before a meaningful task, the host scans at most 200 recent experience rows, excludes
the current task and records newer than its creation, then ranks eligible matches:

- Same task type: 4 points.
- Query topic-term coverage: up to 4 points (generic task verbs excluded).
- Outcome quality: succeeded 1, recovered 0.75, observed 0.25, failed 0.1, unverified 0.
- Recency: `1 / (1 + age_in_days / 30)`.

These form `base`. The final score is
`base * (0.25 + 1.5 * effective_confidence) + similar_help`.
`similar_help = (helpful - misleading) / (4 + helpful + misleading)` uses at most
100 recent automatic evaluations of this lesson in the current confidence generation,
matching the query's task type and normalized topic-term set. It ranges from -1 to 1.
This rewards demonstrated usefulness for the same topic without rewarding exposure.

Distinct topics must share a term; generic requests may match by task type.
This is lexical retrieval, not semantic/vector search. Old experiences outside the
bounded recent window may be missed. Defaults inject at most three lessons and
6,000 ASCII JSON bytes, including source IDs, labels and evidence. Host settings
`ExperienceLimits` allow up to five results, 1,000 scanned rows and 10,000 bytes.
Whole entries are omitted when they cannot fit; context is never padded with all
history. The byte bound also provides a conservative token ceiling.

Experience context precedes current conversation messages and is explicitly labeled
`task_experience`. Current instructions, evidence, tool results, tests and reviews
always override it. `FAILED` and `UNVERIFIED` are cautions, not recommendations.
No experience grants tool access, candidate approval or permission to skip checks.
Normal topic/navigation recall remains a separate route and store.

Potential contradictions are flagged when same-type lessons have opposite verified
success/failure outcomes and their topic sets match or have Jaccard overlap >= 0.5.
Both sides receive `conflicting: true` and up to three counterpart IDs/outcomes.
Detection precedes result and byte limits: even when only one side fits, its conflict
warning remains. This is conservative lexical conflict detection, not semantic
contradiction reasoning; the bounded scan can miss older conflicts. The agent is
instructed to surface conflicts and obtain fresh verification.

## Reuse evaluation and confidence

Schema version 4 adds `experience_quality`, `experience_uses`, and
`experience_evaluations`. Existing lessons start at confidence 0.5 with zero counts.
Reads expose persisted quality and the original outcome-quality weight. Usage is
recorded immediately before the first model call containing the lesson, uniquely
per task/experience, with a frozen metadata-only evidence snapshot. Search/list API
calls do not count as use. A failed provider call can leave a recorded delivery
attempt; it does not earn helpful credit. Casual chat and recall never participate.

After the task's authoritative terminal state commits, the host evaluates each
retrieved lesson exactly once, independently of model final-answer claims:

- **Helpful:** a previously successful/recovered lesson's retained check scopes
  were re-executed and all passed, the task completed, and no final operation failed;
  or the corresponding candidate workflow has a new host-recorded successful gate.
- **Misleading:** a matching final check failed, timed out, or was truncated; or the
  corresponding new candidate review/apply gate failed, including rollback.
- **Neutral:** no matching verification, incomplete execution, inspection alone,
  unrelated failure, or reuse of a historical caution/unverified lesson. Successfully
  avoiding a warned-about failure cannot be inferred automatically.

Check identity includes tool name and the SHA-256 argument-scope hash. Recovery is
recognized only for the same check. Candidate evidence comes from the committed,
evidence-grounded phase reflection created after retrieval, linked through an actual
candidate operation. Merely inspecting a previously passed candidate earns no credit.
Helpful/misleading labels are **outcome associations, not causal proof** that the
lesson improved or harmed the task. Multiple injected lessons are evaluated separately;
the system does not infer which one the model actually followed. Missing capture
evidence yields neutral rather than invented attribution.

The explainable smoothed score is:

`confidence = clamp((3 + helpful_count) / (6 + helpful_count + 1.5 * misleading_count), 0.05, 0.95)`

The six-observation prior cushions a single failure; misleading evidence has 1.5x
weight. Repeated success increases confidence, repeated failure decreases it.
Neutral outcomes and retrieval count never vote. For example, eight helpful uses
produce 0.786; one subsequent failure leaves 0.710, not zero. Original outcomes and
labels never change through feedback. Failed/unverified lessons remain cautions;
their effective confidence used for ranking is capped at 0.5.

For ranking, confidence decays smoothly toward 0.05:
`effective_confidence = 0.05 + (confidence - 0.05) / (1 + age_in_days / 180)`.
Age starts at the last non-neutral evaluation (or lesson creation). Retrieval and
neutral evaluations do not refresh it. Stored confidence is unchanged by the clock;
search returns both stored and effective scores. No lesson is automatically deleted.

Stored metadata includes retrieval/helpful/neutral/misleading counts, confidence,
last-used, last-evaluated, and last-non-neutral (`last_signal_at`) timestamps.
All usage/counter/evaluation writes are transactional. Best-effort learning failures
cannot alter task completion, review, apply, or rollback. Interrupted tasks can have
pending unevaluated uses; there is no speculative backfill. Repeating evaluation is
idempotent, including after restart.

## Security and APIs

The production authentication middleware protects all experience endpoints:

- `GET /experiences?limit=20` (maximum 50)
- `GET /experiences/search?q=parser&limit=3&context_bytes=6000`
- `GET /experiences/{UUID}`
- `DELETE /experiences/{UUID}`
- `GET /experiences/{UUID}/evaluations?limit=20&before=SEQUENCE` (maximum 50;
  returns quality, evaluation events, and a `next_before` pagination cursor)
- `POST /experiences/{UUID}/feedback` with
  `{"verdict":"helpful"|"misleading","request_id":"UUID"}`
- `POST /experiences/{UUID}/confidence/reset`

Blank or NUL-containing search text returns a validation error (HTTP 422).

Feedback is explicit authenticated owner testimony, separately labeled from
automatic evidence. It has the same bounded vote weight; no free text or model tool
can submit ratings. A caller-generated request UUID makes retries idempotent; reuse
with different feedback is rejected. A new UUID is a new intentional owner vote.
This interface is also the explicit user-correction channel; ordinary chat prose is
not mined for ratings. Reset starts a new generation, clears all counters/timestamps,
and restores 0.5 without deleting the lesson or audit history. Old pending tasks and
pre-reset evidence cannot repopulate the reset generation. Deleting a lesson explicitly
cascades its derived quality, use, and evaluation records, leaving task audits intact.

No API or model tool can author arbitrary lessons. Deletion affects only the lesson,
not conversations, memories, tasks or audits. Authentication remains the existing
single-owner model, not multi-user isolation. Local development follows the existing
development authentication policy.

Whole goals containing recognized credentials, protected paths, raw tool-result
markers, private keys or current authentication/provider secret values are excluded
before truncation. Reads recheck goal safety. Tool arguments are hashed, and output
text is discarded, so protected tool content cannot enter through those channels.
Conservative text filtering cannot identify arbitrary unlabeled secrets in user
prose; never put credentials in task goals. Historical lessons are untrusted data,
not executable instructions. No filesystem or Docker privileges were added.

Experiences live in the same external SQLite state volume and use the existing
[backup/restore procedure](deployment.md). Restart preserves them automatically.
The UI is unchanged. Conversation history records dialogue; durable memories are
explicit user/project facts; audits record authoritative events; experiences are
small, derived operational lessons with deliberately narrower evidence claims.

Next milestone: a held-out task replay benchmark comparing retrieval enabled/disabled,
calibrating these association scores and conflict thresholds against measured task
outcomes before adding semantic retrieval or automated retirement.
