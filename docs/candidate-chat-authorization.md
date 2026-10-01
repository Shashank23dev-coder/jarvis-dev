# Owner development capabilities

The single-owner authenticated HTTP perimeter issues a structured
`OwnerDevAuthority` for `engineering_development` in `configured_dev`. Development
HTTP requests need a configured owner token too; unauthenticated local HTTP does
not grant engineering authority. Trusted operator code can issue the same bounded
capability explicitly. It is never a model tool argument or inferred from text.

Candidate creation, inspection, tests, review, discard and job controls depend on
that capability. Owner conversations can inspect and manage all configured-dev
jobs and candidates by ID. Conversation/task IDs remain audit and idempotency
metadata. There is no natural-language authorization parser or intent classifier.
The assistant still follows user scope and plan-only instructions.

The private broker mailbox accepts only typed requests from the trusted adapter;
it is not a public endpoint. The adapter checks the in-process authenticated owner
before writing. The host authenticates the fixed docker-exec/private-UID mailbox
channel separately from request fields; serialized owner fields grant no authority.
Direct broker calls require a trusted in-process owner context. The broker pins
every operation to `/home/ubuntu/jarvis-dev`. No request can name a different root,
shell command, executable, credential location, production target or Docker image.
Missing authority, invalid metadata or unavailable broker fails closed.
The daemon fills authority audit metadata after receiving through its private
channel, allowing the previous production protocol to continue during a dev-only
broker upgrade. Public model/tool arguments still cannot issue that context.

Engineering providers return structured proposals with no tools. Protected root,
security and workflow implementation files remain outside candidate editing scope.
Production files, production env, SSH/AWS/deployment credentials and trading risk
controls remain inaccessible. Proposed secret-like content is rejected. Inspection
redacts credential-like fixture assignments instead of rejecting otherwise approved
source; this does not relax edit validation or protected-path checks.

Tests run in the hardened Docker sandbox. Worktree fingerprints bind tests and
independent review to the actual diff. Worker leases, a process-inherited flock,
bounded attempts and revision budgets prevent overlapping execution. Owner
candidate management shares that lock and refreshes job evidence.

Candidate apply and production deployment retain their existing separate exact
human approval contracts. Development capabilities grant neither. Production has
no host checkout mount, Docker socket or arbitrary shell. Nothing deploys
automatically. New code becomes available to production chat only after an
approved release; restarting the dev broker does not update a production image.
