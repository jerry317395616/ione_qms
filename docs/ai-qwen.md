# Flow and Qwen production configuration

## Supported baseline

- Flow app: pinned `develop` commit for each production release.
- Provider: `openai`.
- Flow model title: `I-ONE Qwen 35B`.
- Model ID: `openai/qwen3.6-35b-a3b-fp8`.
- Qwen: OpenAI-compatible chat completions with tool calling and streaming.

Flow Provider and Flow Model are the model configuration source of truth.
The approved IONE Agent Release stores a secret-free snapshot of the effective
provider name, enabled state, base URL, provider `extra_params`, model params,
merged effective params, and exact clean Git HEADs for IONE QMS, Frappe, Flow,
and installed AI-runtime-affecting apps (`ione_core` and Drive when present).
Any configuration, dependency-commit, or dirty-worktree drift invalidates
execution. A configured `ione_qms_commit_sha` is accepted only when it matches
the readable checkout HEAD. The API key remains only in Flow's encrypted
Password field and is never copied into the release artifact.

## Network and authentication

The model must be reachable from the Press bench network through an approved
internal address. `127.0.0.1` in a bench container points to that container,
not the model host.

Production requires:

- model port bound only to an internal interface or protected by firewall;
- gateway or vLLM API-key authentication;
- an API key stored in Flow Provider `api_key`;
- anonymous `/v1/models` returning 401 or 403;
- TLS or mTLS for any path crossing an untrusted network;
- rate limits, request size limits, timeout limits, health checks, GPU and
  queue alerts;
- no complete prompt or response logging.

Set `IONE AI Settings.allowed_model_hosts` to a JSON array such as:

```json
["10.144.133.1"]
```

Then run:

```bash
bench --site <site> execute ione_qms.ai.readiness.check_qwen_readiness
```

The command sends no clinical content. It verifies host allowlisting,
authentication enforcement, model discovery, and exact served model ID.

## Agents

Installation creates five disabled, app-owned Flow Agents, five dedicated
service users, and five Draft policies:

- `IONE Quality Policy Agent`
- `IONE Medical Record QC Agent`
- `IONE Indicator Analysis Agent`
- `IONE Rectification Agent`
- `IONE Quality Report Agent`

None is production-enabled by installation or migration. Activation requires
role/scope assignment, a complete typed test suite, a current independently
approved evaluation receipt, and release approval. Default `max_iterations` is
eight.

Installation also creates `IONE-EVAL-THRESHOLDS-V1` as a conservative **Draft**
threshold policy. A named Agent Reviewer, QC Reviewer, or Medical Affairs user
who did not create it must approve it. Approval freezes and signs its version,
all ten required categories, and thresholds. At most one threshold policy can
be Approved.

The production evaluation suite must contain all categories:

- accuracy, recall, false positive, evidence consistency, hallucination, and
  expert adoption;
- cross-scope tool overreach, forbidden tool, PII canary leakage, and missing
  evidence.

Each case declares a namespaced synthetic tenant fixture, structured expected
output/evidence/citations, closed tool-argument schemas, expected denials, and
forbidden tools. The evaluator uses the real Flow `Model` and in-memory
`Agent`, the live Flow/Pydantic tool schemas, and the actual IONE
policy/argument/scope/evidence functions. It creates a synthetic task, Flow
session, and Flow run inside a per-case savepoint. `commit` is disabled,
after-commit callbacks are restored to their pre-case state, the savepoint is
always rolled back, and the worker verifies that fixtures, access receipts,
drafts, candidates, task, session, and run did not survive. Any Flow version
that attempts to commit fails closed; set
`ione_ai_evaluation_isolation_mode=dedicated-test-site` with the explicit
`ione_ai_evaluation_dedicated_test_site=1` marker until that version is proven
safe. A reviewed compatible build may use `rollback-only`.

No raw prompt, output, tool argument, tool result, citation text, fixture
identifier, or canary is retained in the evaluation receipt. Only bounded
counts, safe names/reason codes, and cryptographic hashes are retained. Each
passing case includes the hash of the exact runtime prompt actually sent to
Flow; those case projections are themselves covered by the result and receipt
hashes.
Production permits zero successful/unexpected cross-scope calls, forbidden
tool calls, PII leakage, or missing evidence. Evidence references must exist,
belong to the exact synthetic task/policy, pass the ordinary IONE receipt hash
checks, and match task scope.

Every receipt binds the exact Agent Release and checksum, model/provider
configuration, evaluation prompt, Agent instructions, live tool schemas,
policy snapshot, KB source/sync/embedding-settings manifest, test-suite hash,
approved threshold policy hash, evaluator source hash, category confusion
matrix, and result hash.
Release, policy, Agent, model, threshold, and suite rows remain locked through
execution and receipt commit, then all bindings are recomputed. Review repeats
those locks and checks. In production, the newest current-context evaluation
must be Passed and Approved; a newer failed/pending evaluation or any
model/prompt/schema/policy/KB/suite/threshold/code drift denies execution.

## Embeddings and knowledge

The chat model endpoint must not be assumed to support embeddings. Flow
knowledge is production-ready only when an approved internal embedding model
returns vectors through `/v1/embeddings`, Flow records a non-zero dimension,
Chinese retrieval quality passes the approved test set, and private LanceDB
files are included in backup/restore.

Until then:

- do not bind QMS-sensitive knowledge bases to agents;
- use permission-checked realtime imported tools for standards and records;
- keep any RAG-dependent feature flag disabled;
- do not claim knowledge-base acceptance.

Knowledge bases must be split by authorized scope. Flow does not re-check each
chunk's source record permission at retrieval time.

## Patient-data minimization and output DLP

The medical-record tool accepts only unique paths from the release-controlled
`ione-qms-medical-record-ai-profile-v1` semantic profile. Before any value can
reach Qwen, it recursively removes identity-bearing keys and known patient,
encounter, source-record and birth-date values, rejects residual phone,
identity-number, email, address, labeled clinical identifier, external link,
binary, hidden-control and oversized content, and caps both section and total
size. `IONE AI Data Access Log` retains only the profile version and hashes of
the de-identified sections, never the section plaintext.

The same DLP gate runs on task input, every model-created draft/finding, every
Flow Run output field, and every assistant/tool message. Production acceptance
must use canary patient names, source IDs, encounter IDs, dates, phones,
addresses and emails and prove that no canary appears in the prompt, tool
trace, Flow tables, QMS logs, errors or retained artifacts.

## Failure behavior

- Model timeout/unavailability marks the task failed or retryable.
- Formal findings, approvals, penalties, rectification state, and source
  systems remain unchanged.
- Partial drafts remain clearly marked and linked to the failed/paused run.
- Unknown tools, invalid JSON arguments, excessive record counts, out-of-scope
  identifiers, and prompt-injection attempts fail closed and are audited.
- Scheduled aggregate reports are inspected outside the draft tool after the
  exact Flow Run fence is proven. The gate scans `Flow Run` output, tool calls,
  questions and error fields plus every assistant/tool message content,
  tool-call payload and provider-controlled call ID. It also runs after
  confirmation resumes and during terminal-orphan reconciliation. A direct
  identifier, hidden control character, malformed surface, oversized surface,
  or scanner failure changes the run to a content-free quarantined failure,
  clears every model-controlled trace, removes only unreviewed drafts created
  by that execution attempt, and appends a deterministic contained privacy
  incident. Neither a final answer that skips the draft tool nor an unknown
  tool call can bypass this outer gate.
- Initial and Resume model calls first commit a phase/attempt/token/lease claim.
  Two workers cannot finalize the same attempt; a superseded result is rolled
  back without linking or business writes.
- Every approval Resume RQ job is fenced by the next execution attempt. A job
  left in Started/Queued state for an earlier confirmation cannot deduplicate
  or resume a later confirmation.
- Recovery renews a claim only when its original RQ job is still active,
  reconciles exact terminal orphan runs, and accounts for abandoned Running
  runs owned by already-final tasks.
- Scheduled report task insertion, the schedule runtime pointer, and
  enqueue-after-commit registration are one transaction. The task hook is
  suppressed until that pointer is durable; a full rollback clears a partially
  registered callback. A bounded five-minute sweep re-enqueues persisted
  `Pending` tasks with the same deterministic job identity if the post-commit
  Redis enqueue was lost.
- Reviewed approvals and the resumed Flow revision are consumed/finalized in
  one transaction. An expired Resume lease fails closed and never
  automatically repeats an approved write tool.
- Approval expiry/cancellation is committed before best-effort reconciliation;
  each reconciliation unit uses a database savepoint so one malformed legacy
  row cannot roll back already-recorded expiry decisions.
- The same resumed Flow Run can have multiple append-only QMS revisions, one
  per terminal iteration. Full pending/actual tool trace evidence is covered by
  immutable artifact hashes even when `tools_used` lists only executed calls.

The configured RQ timeout, model/gateway timeout, and 30-minute execution lease
must be validated together for the target Press worker. The job timeout must
expire before claim recovery can treat an inactive job as abandoned; live jobs
must continue to receive bounded lease renewal.

## Compatibility gate

Re-run the contract suite whenever Flow, Frappe, LiteLLM, Qwen image/model,
`ione_core`, or any IONE tool schema changes. The suite covers non-streaming,
streaming usage, tool calls, confirmation and resume, service-user scope,
global approval overrides, duplicate claims, long-running live jobs, initial
and Resume crash windows, approval consumption, superseded rollback,
multi-revision Flow Runs, orphan/abandoned-run recovery, injection cases, and
log redaction. Retention tests must also prove terminal-task gating, independent
approval/access-receipt hash checks, pending-approval preservation, and
fail-closed rejection of governed Flow attachments.
