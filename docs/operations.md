# Operations runbook

## Production-mode gate

Do not edit `IONE System Settings.production_mode` directly. Follow the
evidence, independent approval, and third-operator activation procedure in
`docs/production-activation-governance.md`. Missing, expired, revoked, or stale
evidence fails closed at runtime. Emergency deactivation clears production,
real-time rules, and AI together; use the governed Desk button/API when
possible so an append-only activation event records the reason and actor.

## Service ownership

Assign named owners for application, clinical rules, integration, database,
security, AI/model, and incident response. On-call contacts and escalation
times are maintained outside Git.

## Health checks

Monitor:

- public and internal Frappe health;
- web, Socket.IO, scheduler, short/long/AI workers;
- Redis availability, memory, evictions, queue depth, and oldest job;
- MariaDB connections, locks, replication/binlog, storage, and slow queries;
- Press agent, registry, build capacity, and deployment status;
- event/message error rate, retry backlog, reconciliation lag, and rule p95;
- finding/indicator scheduled-job freshness;
- Qwen authentication, health, GPU memory/OOM, request queue, tokens,
  latency, timeouts, and error classes;
- AI initial/resume queue depth, claim heartbeat/lease age, abandoned Flow
  runs, orphan reconciliation errors, and superseded-result counts;
- `IONE Migration State` revision/status, last-batch age, batch count,
  continuation sequence, full source/DocType/plan fingerprint, fixed error
  code, app version, and completion-receipt verification;
- filesystem, object storage, backup age, and restore verification.

Default alerts:

| Signal | Warning | Critical |
| --- | --- | --- |
| Filesystem usage | 75% | 85% |
| Realtime rule p95 | 2.5 s | 3 s |
| Page p95 | 1.5 s | 2 s |
| Scheduler freshness | 10 min | 20 min |
| Integration backlog | site-specific | RPO at risk |
| Backup age | 12 h | 24 h |
| Model error rate | 2% | 5% |

## Safe degradation

- Disable AI features when Qwen is unavailable; deterministic rules continue.
- Pause a faulty source endpoint without deleting its messages.
- Put a faulty rule version into shadow/retired status through clinical change
  control.
- Stop scheduled bulk calculation when database pressure is high; realtime
  critical events remain prioritized.
- Never retry non-idempotent work without checking its business key.

## Backup

At least:

- MariaDB full backup plus binlog for RPO;
- site configuration including encryption key through a separately protected
  channel;
- public and private files;
- Flow LanceDB private files when knowledge is enabled;
- Press release metadata and exact application commits;
- append-only IONE migration completion receipts and their matched state;
- external object-store manifests and integrity hashes.

Backups are encrypted, offsite, access-controlled, monitored, and tested.
“Backup succeeded” is not sufficient until database, config, public files,
private files, secrets recovery, and application startup are restored in an
isolated environment.

## Restore rehearsal

1. Select a production-equivalent backup and record its timestamp.
2. Create an isolated bench/site with the same pinned release.
3. Restore database, site config, public/private files, and encryption key.
4. Run migrate in the isolated site.
5. Verify login, role boundaries, representative clinical evidence, Flow
   passwords, integrations disabled, and knowledge index/rebuild.
6. Reconcile counts and sampled hashes.
7. Measure RPO and RTO.
8. Destroy or securely sanitize the isolated sensitive environment according
   to policy.

## Incident handling

Preserve logs, release metadata, hashes, job IDs, relevant records, and system
time. Do not edit source records or delete evidence during investigation.
Contain the smallest affected component, communicate clinical impact, and
record every operator action. Security incidents involving secrets or patient
data require credential rotation and privacy response.

## Maintenance

- Use Press releases and deploy candidates, never live-container code edits.
- Freeze exact commits and dependencies for each production release.
- Use reversible schema migrations and tested patches.
- Verify manager and every site sharing its bench.
- Complete backup and rollback gates before applying the candidate.
- Keep initial and Resume AI work on the same configured queue. Validate the
  RQ timeout, model/gateway timeout, and claim lease together; recovery may
  renew only a job that RQ still reports active.
- Verify Frappe `Log Settings` retains `Flow Session` for at least
  `max(input_retention_days, output_retention_days) + 7` days. Installation and
  migration may raise this value but must never lower a longer policy. IONE
  extends the Flow Session log controller: ordinary Flow sessions retain the
  official cleanup behavior, while a governed session remains on deletion hold
  until every run belongs to a terminal task, both payload directions and
  transcript/tool surfaces carry the verified retention markers, all immutable
  receipt hashes remain present, and no attachment exists.

AI redaction is intentionally fail-closed. The job must select the latest
immutable Flow revision, require the governed task to be terminal, and validate
exact payload and artifact hashes before redacting. Missing/mismatched evidence
is logged only as a sanitized failure and leaves data untouched. Input
redaction includes user/tool messages, questions, tool traces, bounded
access-log evidence snapshots, and terminal `Consumed`/`Expired` approval
arguments/prompts. Pending or resumable approvals are untouched. Access
snapshots require their content and original record hashes; approvals require
independent argument/prompt hashes. Legacy approvals without a prompt hash,
partial markers, and any governed Flow attachment fail closed and require an
operator-reviewed incident; the job does not delete attachment Files. Output
redaction includes assistant messages even when the Flow Run output field is
empty. Lineage, hashes, access receipts, approval provenance, revision records,
and audit comments remain.
The deletion hold has no seven-day escape hatch: a missing/mismatched link,
partial marker, legacy attachment, or verification exception preserves the
entire governed Flow Session until the incident is resolved.

Before enabling scheduled indicators, run the bounded, read-only
`ione_qms.services.versions.audit_indicator_effective_periods` function with
`bench execute`. Resolve every `INVALID_EFFECTIVE_BOUNDARY` and
`OVERLAPPING_EFFECTIVE_INTERVAL` through governed replacement/retirement;
never rewrite or delete historical results. Continue with `next_cursor` while
`has_more` is true.

Also verify that each Published Indicator Version has an Active source mapping
covering its full effective interval and frozen source-system, mapping,
query-contract, and calculator-code hashes. A recalculation with an unchanged
input receipt is idempotent; a changed source manifest or execution input must
append a new revision and atomically advance `IONE Indicator Result Pointer`.
Use the POST-only endpoint below for an authorized current result:

```text
POST /api/method/ione_qms.api.indicator.verify_indicator_result
result=<current-result>&reproduce=0
```

Historical R1-after-R2 reproduction is never a bare console/read call. A named
Auditor, QC Reviewer, or Medical Affairs user first POSTs
`request_historical_indicator_verification` with an exact purpose, source-row
limit, use limit, and TTL. A different named QC Reviewer or Medical Affairs
user POSTs `review_historical_indicator_verification`. The original requester
then POSTs `verify_indicator_result` with `reproduce=1` and the approved
authorization name. Every consumed attempt advances the finite use count and
appends an HMAC-signed `IONE Indicator Historical Audit Access Receipt`.

`verified=false` with a drift reason is an investigation signal, not permission
to edit history. Before scheduler activation, the migration repair must report
zero `Legacy Quarantined` results, zero blocked calculation receipts, and zero
Verified series without a current pointer. Resolve legacy data through governed
replacement/retirement; do not synthesize source manifests or pointer history.

## Post-migrate completion gate

Press migrate intentionally schedules legacy data repair on the long queue
instead of holding the deployment transaction open. Each worker owns a
site-level advisory lock, performs at most five bounded passes by default,
commits every pass independently, and enqueues a revision/sequence-scoped
continuation after commit while finite work remains. The hourly scheduler
repairs a lost continuation. Re-running the worker is idempotent:

```text
bench --site manager execute ione_qms.tasks.migrations.run_post_migrate_backfills --kwargs '{"batch_size": 200, "max_passes": 5}'
```

`IONE Migration State` has these operational meanings:

- `Pending`: more bounded work exists and a continuation should be queued;
- `Running`: a worker started the current revision;
- `Blocked`: an ambiguous duplicate/missing parent requires governed data
  review; no completion receipt was issued;
- `Failed`: the last worker failed and recorded only a fixed non-clinical
  error code;
- `Completed`: the linked append-only receipt matches the schema revision,
  current app version, complete deterministic fingerprint, canonical
  sanitized summary, committed batch count, completion timestamp, canonical
  record hash, and the indicator-artifact manifest format, exact count,
  SHA-256, and creation high-watermark.

The indicator legacy audit pages by an ordinal inside that frozen manifest; it
does not use an artifact name as an authority cursor. Before issuing completion
it recomputes the same manifest, so a lower-sorting row restored during a scan
forces a restart instead of being skipped. Manifest capture and audit reads use
pages of at most 500 rows; no page materializes an unbounded result set.
Restored legacy rows whose `creation` is NULL or empty occupy a separate
first segment of every DocType population, so they are bound and audited exactly
once instead of falling outside the timestamp predicate.
Calculation, Result, and Detail entries bind a recomputed immutable payload
fingerprint, not a stored checksum copied on trust. Mutable Calculation outcome
and Pointer state are live-verified on every gate check.

Runtime receipt verification recomputes the frozen population and requires zero
unresolved quarantine receipts. New post-watermark artifacts are permitted only
when their full governed integrity verification succeeds (or an exact
append-only quarantine disposition already approves them). Adding or restoring
an unverified legacy calculation, result, detail, or pointer immediately makes
the completion gate false and keeps production workloads disabled until the
governed migration/disposition process converges again.
During the audit's final consistency check, an exact verified quarantine receipt
may terminate the scan as `Blocked`; it cannot satisfy the production gate until
its governed disposition is approved.

Each immutable completion receipt is an artifact epoch. Its key is the current
code/metadata fingerprint key plus SHA-256 of the canonical manifest binding.
If a transaction that was invisible at the first completion later commits, the
old epoch fails closed; after the newly visible row is audited and resolved, the
worker appends a distinct receipt linked to the prior receipt. It never edits or
reuses the stale epoch.

Inspect the `definition_lineage` component in every repair-pass summary.
`has_more` must be false and `blocked` must be zero. The repair may reconstruct
semantic/authority snapshots only for Draft Standard, Rule, and Indicator
versions. Every ambiguous reviewed legacy version is marked `Quarantined` and
gets an append-only `IONE Definition Lineage Receipt`; the migration does not
guess an old Standard Version or Clause. A quarantine in Under Review, Expert
Review, Test, Historical Replay, Shadow Run, Approval, Approved, or Published
blocks completion. A named QC Reviewer or Medical Affairs user must retire it
with an explicit end date and create a new verified replacement. Never clear
`lineage_status` or edit the receipt directly.

Treat every state other than verified `Completed` as not ready. Do not edit the
state or receipt, forge a receipt, or bypass the gate. Resolve a `Blocked` row
through the owning governed workflow, retain the sanitized Error Log
reference, then let the scheduled worker retry. AI and real-time rules remain
off at runtime, and source/endpoint plus production switch validation rejects
enablement, until the current receipt verifies.

Before accepting the Press candidate, prove a two-or-more-batch continuation,
a worker restart, a duplicate enqueue, a blocked-row recovery, and a tampered
receipt rejection. Capture the state and receipt as release evidence without
clinical row identifiers.

The revision and receipt key are not hand-maintained release numbers. They are
derived from SHA-256 over normalized shipped Python, canonical DocType JSON,
the ordered migration plan, and `ione_qms.__version__`. A code, metadata, plan,
or version change automatically makes the old receipt stale. Runtime AI,
integration, deterministic quality/indicator processing, reporting,
retention, analytics, recurrence, export, and other non-migration scheduled
work all fail closed until the new receipt verifies.

## Durable indicator and analytics batches

Scheduled indicator and monthly analytics work uses four service-managed,
native-export-disabled audit records: `IONE Batch Run`, `IONE Batch Work Item`,
`IONE Batch Recovery Receipt`, and `IONE Batch Source Epoch`. Generic Desk,
REST, import, cancel, delete, and Administrator mutation are not recovery
paths. Monitor run status, phase, heartbeat/lease expiry, retry time, bounded
attempt counts, work totals, manifest/verification counts, and receipt hashes.

Every run has a deterministic 64-character identity, an allowlisted task, a
database advisory lock, a renewable 15-minute lease, at most eight run/work
attempts, exponential retry capped at one hour, and an append-only work receipt
hash at completion. The recovery scheduler requeues Pending/due retries and
recovers a stale wall-clock lease only when the database advisory lock is no
longer owned. Duplicate enqueue or worker overlap must not execute a second
authoritative page.

Indicator dimension jobs perform materialization, verification, execution, and
final verification. The first and second scans must have identical ordered
manifest hashes and counts; the final scan and freshness watermark must still
match while the ordered source-epoch locks are held. Otherwise the run becomes
`Superseded` and a new deterministic generation is dispatched. A completed
indicator generation advances the `IONE Indicator Result` epoch in the same
transaction that makes its results analytics-visible.

Analytics freezes a signed `(creation, name)` high-water contract, processes
at most 1,000 source rows per transaction, records one deterministic successful
fact receipt, and then cleans stale derived facts in pages of at most 500 while
holding the source epoch. Any source drift supersedes the run; no mixed
generation may complete. Daily reconciliation covers 13 months by default,
while indicator late-data revalidation covers the governed 400-day window.

After eight failed attempts, a run is `Dead Letter`. Only a named human with
`System Manager` or `IONE QC Administrator` may call the POST-only
`ione_qms.services.batch_reliability.recover_dead_letter` method. `Guest`,
`Administrator`, and `IONE Agent Service` are explicitly rejected. The reason
must contain 20–500 characters; recovery resets only the failed work/run retry
state and appends an immutable receipt bound to actor, timestamp, reason hash,
and contiguous recovery sequence. Never edit `recovery_generation`, work
payloads, or receipt rows.

## Install-hook failure boundary

`before_install` is a read-only collision audit and runs before IONE schema
sync. It rejects unknown IONE-owned names and incompatible shared
Workflow-State/Action or Flow-retention records without writing. Frappe then
synchronizes schema, records the app as installed, and commits before
`after_install`. Each `after_install` reconciliation step is idempotent, but a
failure at that stage is still a partial installation with committed external
state. On Press, stop the candidate and restore the exact pre-install database,
configuration, public files, private files, and prior candidate. Never remove
the installed-app row, run generic uninstall, or manually claim colliding
shared objects.

## Monthly report terminal recovery

A terminal scheduled Quality Report is never overwritten and its month is
never skipped. The schedule remains on that exact closed calendar month until
the report task completes with a governed draft. Only the schedule's configured
independent `report_reviewer` may use the POST recovery action. Each action
must carry the terminal `prior_task` visible on the reviewed schedule, so a
delayed duplicate returns the original authorization instead of consuming the
next recovery slot. Each new action
creates an immutable `IONE AI Report Recovery Authorization` bound to the
approved schedule checksum, snapshot, month, prior terminal task/status/dispatch
key, reason, actor, timestamp, and contiguous sequence. The replacement task
has a distinct deterministic dispatch key and links both the prior task and the
receipt.

At most two replacement tasks are allowed per schedule/month. A terminal
second replacement creates one idempotent, sanitized Availability incident and
leaves the schedule failed. Operators must investigate that incident; they
must not edit old tasks/receipts, advance the month, or rerun a task through
generic Desk/REST CRUD. Resuming reporting beyond the governed limit requires
the separately approved controlled-backfill or replacement-schedule process.
This coded path still requires migrated-Bench concurrency, queue, Qwen,
permission-negative, incident, and browser UAT before production enablement.

## Integration tenant migration gate

The post-migrate worker generates a stable namespace for legacy Source Systems
but never guesses clinical scope. It disables every enabled Source System
without an approved `allowed_scopes` row and every Endpoint that is unscoped
or whose Source is disabled. The same bounded, re-entrant pass disables an
enabled Endpoint whose governed Mapping is missing, inline/ambiguous, inactive,
out of period, source/endpoint-mismatched, incomplete, snapshot-invalid, or
checksum-invalid. Inline `connector_options.master_data` is treated as a second
mapping truth and also disables the Endpoint; the migration never copies or
guesses it into a Mapping version. It records only a fixed non-clinical reason
code and never logs or copies Mapping content. Before re-enabling:

1. Obtain accountable hospital approval for every Source and Endpoint scope.
2. Configure the Source scope, then an Endpoint subset, using a named
   `IONE Integration Administrator`.
3. Create a source/endpoint-bound Mapping version from the approved field
   dictionary and reviewed `master_data_json`, activate it with an effective
   date, clear both legacy inline scope mapping and
   `connector_options.master_data`, and link the exact version to the
   still-disabled Endpoint. Never copy or infer an ambiguous mapping.
4. Resolve all legacy Patient/Encounter tenant fields through an approved,
   separately reviewed migration; ambiguous cross-hospital patients remain
   quarantined and are never assigned automatically.
5. Run negative H1-to-H2 Push, Pull, retry, replay, mapped-Link, mapping-version
   switch, frozen scope/master-data retry, and checksum/snapshot-tamper tests.
6. Confirm invalid/oversized/out-of-scope sentinel PHI appears nowhere in the
   Integration Message body, Data Quality Issue, Error Log, or worker log.
7. Confirm Endpoint raw cursors are unavailable through ordinary Desk/API
   access and Integration Jobs contain only cursor SHA-256 fingerprints.

### Identity HMAC rotation runbook

Use the two-phase overlapping-ring procedure in
`docs/privacy-identity-access.md`; never perform a direct old-only to new-only
rolling switch. Phase 1 keeps the old key active while every worker learns the
future key as retained. Phase 2 makes the future key active and retains the old
key, followed by another complete worker restart. Record the configuration
fingerprints and worker restart evidence for both phases. Before starting,
verify both exact-case `stable_source_key` unique constraints exist. During the
roll, new workers hold candidate and hashed stable-source locks through outer
commit/rollback; the database constraint on the length-delimited stable tuple
fingerprint remains the final cross-version arbiter for an older draining
worker.

After phase 2 is fully converged, run the bounded
`ione_qms.services.identity_keys.backfill_site_identity_keys` maintenance call
in batches no larger than 1,000. Commit and record each returned summary.
Continue until `has_more=false` and `blocked=0`; run one additional pass and
require `updated=0`. A blocked row, unknown contract, duplicate target, or
remaining non-active Patient/Encounter contract stops the change. Do not
remove the old key, relax validation, or hand-edit a surrogate. Close the
rotation only after contract-count audit, cross-version worker concurrency
test, backup, restore proof, rollback-window expiry, and security-owner
approval.

If a retained key is removed prematurely, ingestion must find the row by its
exact-case, length-delimited `stable_source_key` fingerprint and stop on
cryptographic verification; it must not insert a replacement. Restore the
retained key, finish the bounded rekey/audit, and retry the source message.
Treat any stable-fingerprint uniqueness migration failure as evidence of
pre-existing corruption and reconcile it before continuing.

## Standard authority archival

Set `ione_standard_archive_allowed_hosts` in site configuration to an explicit
JSON list (or comma-separated string) of approved public authority hosts. The
archive endpoint accepts credential-free HTTPS on port 443 only, revalidates
every redirect, rejects any DNS answer that is not globally routable, pins each
TLS connection to the exact validated address while verifying the original
hostname, enforces the 50 MiB limit, and writes an attached private File. Only
signature-verified PDF, macro/external-link-free DOCX, and UTF-8 plain text are
accepted; HTML, SVG, executable content, generic binary text, and extension
mismatches fail closed. Keep the allowlist empty
to disable remote archival; a reviewer may instead upload an attached private
source File. A URL alone can never enter verified review.

## Agent production-evaluation isolation

Evaluation is disabled unless site configuration explicitly selects one mode:

```json
{
  "ione_ai_evaluation_isolation_mode": "rollback-only"
}
```

Use `rollback-only` only after the exact frozen Frappe/Flow candidate proves
that the in-memory Flow Agent performs no implicit commit. The evaluator also
blocks `frappe.db.commit`, snapshots transaction callbacks, gives every case a
savepoint, always rolls it back, restores the original callbacks, and verifies
zero retained fixture/runtime/artifact rows. A blocked commit or unknown
callback implementation is a failed evaluation, never a skipped check.
The runtime Agent is always built with `auto_approve=False`. If its release
contains any confirmation tool, queueing in `rollback-only` mode fails before
model execution: a savepoint cannot prove the real committed
pause/independent-review/resume protocol.

If the exact Flow candidate cannot meet that contract, use a separate
synthetic-only site and both settings:

```json
{
  "ione_ai_evaluation_isolation_mode": "dedicated-test-site",
  "ione_ai_evaluation_dedicated_test_site": 1
}
```

Never set the dedicated marker on a site containing clinical records. The
dedicated site must use the exact App Releases, model/provider configuration,
Agent Release, policy/scope fixtures, KB revision, and evaluator commit under
review. Do not copy a raw model transcript, fixture, or database row into the
production site. Dedicated-site output is QA evidence only; it cannot authorize
production in this release. Production remains fail-closed until the exact
candidate (without confirmation tools) proves rollback-only mode on its own
site, or a separately reviewed cryptographically trusted confirmation-path
receipt-promotion design is implemented.

Before activation:

1. Approve the seeded Draft threshold policy with a named independent reviewer.
2. Supply all ten typed synthetic categories and adversarial cases.
3. Run the evaluation and independently approve its newest Passed receipt.
4. Mutate each bound input in QA and prove the old receipt immediately fails.
5. Prove an allowed tool with hostile scope, a forbidden tool, invented or
   missing citations, and a PII canary cannot pass.
6. Prove a concurrent suite/release edit blocks and then invalidates the
   in-flight result.
