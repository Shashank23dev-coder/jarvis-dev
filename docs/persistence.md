# Conversations, tasks and memory

Jarvis uses SQLite for local persistence. The default application stores data at
`~/.local/state/jarvis/jarvis.sqlite3`. Set `JARVIS_DB_PATH` to an absolute path
outside the checkout to choose another location. The directory is created on
startup. Database paths inside the checkout are rejected so private conversations
and memories cannot enter repository listings or Bubblewrap snapshots. Keep custom
locations outside the Python runtime and system directories mounted by Bubblewrap,
too. Configuration belongs to the host; it is never a model or HTTP argument.

`main.create_app(database_path)` creates an independent app and store. Startup
initializes the database, and shutdown closes it. Imports do not open a database.
For isolated Python callers/tests, `TaskStore()` uses SQLite `:memory:`;
`TaskStore(path)` provides durability. Call `close()` when finished. The former
in-memory store had no saved records to migrate. Existing live process data must
be exported before upgrading if it needs to be retained.

## API

Existing `/task`, `/tasks`, `/tasks/{id}`, `/activity`, `/health`, and `/` routes
retain their response contracts. Tasks add `conversation_id`. Submitting
`POST /task` without that field creates a new conversation; supplying an existing
ID continues that conversation. The agent receives its ordered user/assistant
history through the current task using bounded current-chat context. Explicit
recall-oriented requests also receive selective prior-chat/durable-memory excerpts
as separate labelled data; ordinary chat does not trigger those searches. Tool
outputs and model reasoning are not retrieval sources. See [recall.md](recall.md)
for ranking, source labels, filtering, limits and search APIs. Planning mode still
returns its usual steps.

| Route | Purpose |
| --- | --- |
| `POST /conversations` | Create an empty conversation; no body required |
| `GET /conversations` | List conversation IDs and creation timestamps |
| `GET /conversations/{id}` | Retrieve conversation metadata |
| `GET /conversations/{id}/messages` | Retrieve messages in insertion order |
| `POST /memories` | Explicitly save `{scope, scope_id, content}` |
| `GET /memories?scope=user&scope_id=local` | List explicitly saved memories for one scope |
| `GET /memories/{id}` | Read a saved memory |
| `DELETE /memories/{id}` | Delete a saved memory; return `{deleted: true}` |

Memory scope is `user` or `project`; `scope_id` identifies the user/project in
this local application. It is a namespace, not an authentication boundary.
Missing/invalid resource IDs return 404; malformed payloads return 422. Memory
payloads reject unknown fields. Blank text, NULs and oversized text are rejected.
Task goals and memory content are limited to 32,000 characters; scope IDs to 200.
UTC timestamp formatting remains compatible with the previous API.

The existing browser layout and styling are unchanged. JavaScript retains the
conversation ID in local storage, sends it on subsequent requests, and restores
saved messages after reload. It stores only the ID locally. Agent failures return
an `X-Conversation-ID` header so a first failed request can still be revisited.
Conversation selection and memory management UI are intentionally deferred.

## Schema and writes

Schema version 4 is recorded in SQLite `PRAGMA user_version`. Initialization runs
inside `BEGIN IMMEDIATE`, creates/migrates the schema, and is safe to repeat or run
concurrently. A newer, unsupported schema version fails explicitly. Foreign keys
are enabled on every connection; all statements use parameterized values.
Transactions commit each store operation, serialize access to each connection,
and use SQLite's lock timeout across connections/processes.

| Table | Stored data |
| --- | --- |
| `conversations` | UUID primary key, creation timestamp |
| `messages` | Monotonic sequence, UUID, conversation/task foreign keys, role, content, timestamp |
| `tasks` | UUID, conversation foreign key, goal, status, creation time, steps JSON, response, agent metadata JSON |
| `activity` | Monotonic sequence, task foreign key, existing event JSON including timestamps and tool metadata |
| `memories` | UUID, user/project scope, scope ID, content, creation timestamp |
| `experiences` | Bounded evidence-derived task lessons; separate from memory, with source IDs and a unique task/workflow-phase key; see [experiences.md](experiences.md) |
| `experience_quality` | Smoothed bounded confidence, outcome/use counters, timestamps, and reset generation |
| `experience_uses` | Unique task/lesson delivery attempts with frozen metadata-only evidence |
| `experience_evaluations` | Paginated automatic/owner evaluations and reset history; no raw output or prose |
| `candidates`, `candidate_events` | Isolated self-edit attempts, evidence, limits and ordered stage history; see [self-edit.md](self-edit.md) |

Messages are indexed by conversation and sequence; activity by task and sequence;
memories by scope and scope ID. A unique task/role pair prevents duplicate user or
assistant records for one task. User message, new conversation (if needed), task,
and initial activity are committed together. Agent response/status/metadata and
assistant message are committed together. Store methods refresh compatible task
dictionary views, but callers must use `update_task` to persist changes; mutating
returned dictionaries directly is not a persistence interface.

## History, memory, and experiences

`MemoryStore` exposes `conversation_history`, `task_history` (including activity),
and explicit `create_memory`, `get_memory`, `list_memories`, `delete_memory`.

Conversation history records what was said. Task/activity history records what
Jarvis attempted and its outcomes. Durable user/project memories contain only
facts or preferences deliberately saved through the explicit memory interface.
No automatic extraction or promotion occurs. No memory mutation tools have been
added to the agent registry.

Conversations, messages, task status/steps/responses, safe tool activity and explicit
memories survive restarts. Tool events continue to contain metadata only: no raw
arguments, file content, command output, exception details or reasoning. Existing
legacy planning errors retain their prior API behavior. Protected paths, tool
allowlists, bounds, and read-only Bubblewrap execution are unchanged.

## Limits and next milestone

This remains a single-owner synchronous application. Text-based intent-aware recall
is available through [recall.md](recall.md). There are no embeddings, semantic
search, automatic long-term memory extraction,
background job recovery or resumed tool calls. Explicit isolated self-edit candidates
are now available through a separate [controlled workflow](self-edit.md); the normal
chat tools remain read-only. Interrupted tasks retain their last committed status; a restart does
not resume them. Concurrent conversation requests are ordered by stored message
sequence, without a conversation-level execution queue. Retrieval has bounded search/recent-history APIs. Full-history pagination,
retention/export controls and multi-user authorization remain deferred. Production
authentication is described in [deployment.md](deployment.md).
Deletion removes a memory from the application, not a forensic secure erase or
backup deletion.

Recommended next milestone: explicit conversation selection and memory review/
delete controls using these APIs, plus history pagination and retention/export.
Main-tree changes require the existing exact human-approved apply workflow;
retrieved historical approval never grants current authority.

Run the complete Python suite through `RepositoryTools().run_pytest()` on a host
that permits Bubblewrap, and run `node --test tests/ui.test.cjs` for the UI. Tests
use temporary databases, scripted models and no provider calls.

Schema version 3 adds only `experiences` and its index. Existing conversations, tasks,
memories, candidate records and apply audits are retained unchanged. Experiences
use the same persistent database and backup/restore procedure. They are derived
operational lessons, not automatically promoted durable user/project memories.
Version 4 adds quality, use, and evaluation tables with neutral initial confidence
for existing lessons. Resets preserve evaluation history in prior generations;
explicit lesson deletion cascades only its derived evaluation records.
