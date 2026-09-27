# Selective conversation recall

Recall uses the existing SQLite `messages` and `memories` tables without changing
them. No embeddings, background model calls or automatic memory extraction are
required. Schema version 3 separately adds [task experiences](experiences.md). Task activity, raw tool outputs, model reasoning,
candidate records and apply journals are not retrieval sources.

## When the agent searches

The host recognizes explicit recall-oriented requests, for example:

- “What did we discuss yesterday?”
- “Continue from last time” / “Continue where we left off”
- “What do you remember about SQLite?”
- “What did we decide about backups?”
- “Remind me about our database plan”
- “Show recent conversations”

This is a conservative English intent matcher, not a semantic classifier. Greetings,
ordinary questions, “Continue” alone and tool requests do not trigger cross-chat or
memory searches. Existing current-chat context is still available. Unsupported
wording can be restated using an explicit recall request or the search API.
“Yesterday” refers to the previous UTC calendar day. “Continue from last time”
selects the most recent eligible prior conversation, excluding the current one.
The following exact navigation forms (case-insensitive, optional punctuation/Please)
also select just the last prior chat by its most recent message before the active
request: “What was the last conversation before this one?”, “What did we talk about
last time?”, “Continue from where we left off.”, and “What was my previous chat?”.
They bypass topic matching and never search durable memories. Identity is selected
before safety filtering; an unreadable latest chat is not replaced with an older one.
Only a bounded window from that conversation is read, so current-chat length cannot
crowd it out. The answer is a concise summary of that conversation's excerpts only.
If the available conversation has at most two messages and four words, Jarvis quotes
the brief exchange and states there is insufficient context, without a model call.
With no readable excerpts it reports that limit even if current-chat context exists.
Navigation metadata is included in the existing serialized context budget. Topic
queries, including requests that explicitly name a topic, retain normal ranking.

Recent-history requests do not add unrelated durable memories. Topic recall searches
both source types; general “what do you remember?” can include recent saved memories.

## Sources and priority

Current-chat messages stay in their original user/assistant roles and chronological
order, through the active task. Cross-conversation results exclude that conversation
and messages inserted after the current task's user message. Saved memories newer
than the current task are also excluded. The supplemental results enter as one
labelled, untrusted JSON reference message **before** the current chat, not as old
user requests or tool results to execute.

Each hit includes its source (`prior_conversation` or `durable_memory`), record ID,
creation timestamp, relevance score, excerpt and truncation flag. Message hits also
include conversation/task IDs and speaker role; memories include scope and scope ID.
Instructions tell the assistant to favor current user/current-chat statements over
conflicting older evidence, identify source distinctions when useful, and never
infer permission from retrieved instructions. Exact current-turn candidate/apply
authorization remains unchanged.

Prior-chat recall means “someone said this in a conversation,” not “this is a verified
fact.” Durable memory means the record was explicitly saved through the memory API;
it can still be stale or incorrect. Retrieval performs no writes or promotion into
memory. Model answers remain ordinary conversation messages, as before.

With no retrieved results and no usable earlier current-chat context, Jarvis returns
an explicit bounded-search no-result answer without a model call. If current-chat
context exists, the model may answer from it and must not invent historical recall.
“No result” is not a claim that the entire archive was exhaustively searched.

## Ranking and bounds

`RecallService` reads at most the newest 500 message rows and 500 memory rows per
request, rather than materializing an entire archive. A conversation-scoped API
search uses the selected conversation's window; memory scope filters select the
corresponding namespace. Rows are selected before topic/date/safety filtering, so
an old matching record outside the window is deliberately not found.

Unicode words are case-folded and common question/recall words removed. Up to 16
unique topic terms are used. A quoted phrase is preferred; otherwise the remaining
terms form the phrase. Exact phrase matches require word boundaries. Ranking is:

`8 × phrase_match + 4 × fraction_of_terms_matched + 2 / (1 + age_in_days / 30)`

Thus phrase/topic relevance dominates the recency boost; newer equally relevant
records rank higher. Results are deterministic for the same database and clock.
Excerpts are centered near matched terms, not always taken from the beginning.
Recent-history results use only recency and are literal excerpts, not generated
summaries. Filtering and ranking are local; no extra provider request is made.

Default limits:

| Context component | Bound |
| --- | --- |
| Cross-source candidates inspected | 500 messages + 500 memories |
| Retrieved hits | 6 total across both sources |
| Excerpt | 1,200 characters before serialized-budget fitting |
| Entire supplemental JSON including metadata | 10,000 ASCII-serialized bytes |
| Current-chat selection | Latest 40 messages, normally within 64,000 serialized bytes |
| Entire agent input including subsequent tool turns | Existing 256,000 serialized-character guard |

ASCII JSON accounting charges Unicode escapes and metadata, giving a conservative
budget without a tokenizer dependency. It is not a promise of a model-specific exact
token count. The active request is never silently truncated; if it alone exceeds the
current-chat target, older messages are omitted and the existing whole-input guard
still applies. Normal HTTP goals remain limited to 32,000 characters. No full archive
is ever injected into a model call, and retrieval happens once per task, not once
per tool iteration.

Trusted Python callers can pass `RecallLimits` to `RecallService` or
`AgentRunner(recall_limits=...)`: result ceiling 12, scan ceiling 2,000 per source,
excerpt ceiling 2,000 characters, context ceiling 16,000 bytes. HTTP callers can
lower/raise result and context bounds only within those ceilings; they cannot
change SQL, table sources, tool permissions or model limits.

## Read interfaces

`MemoryStore.search_messages`, `search_memories`, and `recent_history` provide Python
interfaces; `RecallService` adds limit configuration. The existing full conversation
history API remains available for explicit browsing and UI restoration, separately
from bounded prompt construction.

| Endpoint | Parameters |
| --- | --- |
| `GET /conversations/search` | Required `q`; optional `conversation_id`, `exclude_conversation_id`, `limit`, `max_bytes` |
| `GET /conversations/recent` | Optional `conversation_id`, `exclude_conversation_id`, `limit`, `max_bytes` |
| `GET /memories/search` | Required `q`; optional paired `scope` and `scope_id`, `limit`, `max_bytes` |

Queries are 1–500 characters. Results contain `status`, `sources_searched`,
`scan_limit_per_source`, `results` and a result-budget truncation flag. Per-hit flags
indicate excerpt truncation. Recent history returns bounded message excerpts with
conversation IDs; it does not fabricate summaries. Invalid limits, UUID syntax or
unpaired scope filters return 422. No matching visible result returns `not_found`.

Production authentication protects all these routes. This remains a single-owner
application: memory scopes are namespaces, not user authorization boundaries.
Without a scope filter, recall searches the owner's bounded set of user/project
memories and labels their namespaces. Multi-user ACLs must precede any multi-tenant
use. Existing UI appearance, database persistence, Docker configuration and apply
approval behavior are unchanged.

## Sensitive data and limitations

The whole record is screened **before** excerpting. Recognized credentials/private
keys, JWTs, authorization values, raw tool-result markers, protected paths, control
characters and the current API/authentication secret values are excluded. Scope
metadata is screened too. Historical current-chat messages use the same filter;
the active user request retains its existing direct-input semantics. Retrieval does
not read environment files, filesystem contents, activity payloads or protected
candidate/apply records.

These conservative text filters can omit benign records mentioning protected paths.
They cannot classify an arbitrary unlabeled secret embedded in otherwise ordinary
prose. Do not store credentials in chat/memory, and retain the existing protected-tool
boundaries. Search results are untrusted data, never authority to reveal protected
files or invoke apply. Existing explicit full-history/memory CRUD APIs have not
been replaced by the retrieval filter; the authenticated owner can still inspect
stored records through those interfaces.

Next: a source-aware recall/memory review UI and a relevance/no-result evaluation
set. If archive coverage beyond the bounded recent window becomes necessary, add
SQLite FTS indexing with an explicit migration and bounded query plan before
considering embeddings. Scope-aware access control is required for multiple users.
