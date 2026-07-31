# Rule production-safety contract

## Event execution

- Published and historical rule selection never joins mutable parent-rule
  semantics. The rule version pins code/name/type, plug-in key, standard and
  clause lineage, risk, ownership, and description in its checksum.
- Missing legacy semantic snapshots fail closed as an execution error; they
  are never reconstructed silently from the current parent. Create, replay,
  approve, and publish a new governed version instead.
- `IONE Clinical Quality Event.processing_status` is `Completed` only when every selected
  deterministic rule finishes without `Error`.
- An `Error` execution is stored as an append-only `IONE QC Execution`, the event becomes
  `Error`, an idempotent `IONE Data Quality Issue` is created, and authorized reviewers are
  notified. Daily quality scans include both `Pending` and `Error` events, so the event remains
  retryable.
- `Insufficient Data` creates an idempotent data-quality issue and never creates a quality
  finding.
- A failed `Create Finding` action persists one or more append-only
  `IONE QC Finding Evidence` rows in the same transaction as the new candidate finding.
  Evidence includes source lineage, rule version/checksum, standard clause, bounded expected
  and actual value summaries, and a canonical SHA-256 integrity hash.
- The integration-created event queue calls the non-whitelisted internal job entry point.
  The whitelisted manual endpoint retains normal document read-permission checks.

## Historical replay and shadow validation

Call the POST API
`ione_qms.api.rules.start_validation_run` with:

- `rule_version`
- `run_type`: `Historical Replay`
- `window_start` and `window_end`
- `sample_limit` between 1 and 5,000

Only a named `IONE QC Reviewer` or `IONE Medical Affairs` user can start the
run. `Administrator` and service users cannot attest it. The long-queue job
evaluates the pinned rule version inside rollback-only savepoints and does not
create findings or other clinical workflow artifacts.

Each completed job creates one append-only `IONE QC Rule Validation Run`. It records the rule
checksum, event/result counts, source sample hash, ordered per-event digest manifest, bounded
selector metadata, requester, and timestamps. It contains no raw patient payload. Promotion
recomputes every row digest, count, summary, run key, and canonical record hash; a merely
well-formed 64-character value is never accepted as proof.

`Shadow Run` is not an offline replay type. While a lineage-verified version is
in `Shadow Run`, has `shadow_mode` enabled, and the server clock is inside its
frozen observation window, persisted non-manual live-event dispatch evaluates it in a
rollback-only savepoint. Historical Replay and Shadow Run accept only the built-in
deterministic expression DSL. Python plug-ins always fail closed before registry lookup,
enqueue, or execution; a `side_effect_free` declaration is not a security boundary. A
shadow result can only append an
`IONE QC Rule Shadow Execution`; it cannot create a finding, alert,
notification, data-quality issue, or event-processing error. Each receipt
binds the event/context/evaluation hashes, exact rule checksum, current clean
application commit, clinical scope, and observation time.

Named reviewers append at most one `IONE QC Rule Shadow Feedback` per execution and must
record an independent gold label; the assessment is derived from that label and the engine
result rather than selected by the reviewer.
The version freezes the observation start/end, minimum execution count,
minimum feedback count, minimum feedback coverage, minimum Passed/Excluded
stratum samples, and maximum false-positive and false-negative rates before
replay begins. Passed and Excluded review targets are chosen by a site-keyed,
deterministic HMAC rank, so an author cannot hand-pick easy samples. Promotion
waits until the window ends, recomputes every execution/feedback canonical
record hash and logical key, requires all triggering results plus the selected
strata, calculates false negatives only from independent gold-positive labels,
and fails closed on missing legacy contract fields, missing data, rule errors,
or unresolved review.

Workflow gates fail closed:

- `Historical Replay` to `Shadow Run` requires a completed historical-replay artifact for the
  current checksum, at least one evaluated event, and zero rule errors.
- `Shadow Run` to `Approval` requires the completed online window and its
  accountable feedback thresholds for the exact current checksum and executor.
- Publication rechecks historical replay, online shadow evidence, and every
  current test receipt. These technical artifacts do not set clinical approval
  metadata and cannot substitute for the configured human workflow roles.

## Governed rule tests

Every rule version must cover all seven sample types: Positive, Negative,
Exclusion, Boundary, Missing Data, Real World, and Regression. Positive,
Negative, Exclusion, and Missing Data samples have fixed expected outcomes;
an execution `Error` can never be accepted as passing.

Saving any test-definition change canonicalizes the input, recalculates its
definition hash, and clears all projected result fields. Tests run only while
the version is in `Test`, under deterministic version/test row locking, and
append an immutable `IONE QC Rule Test Execution`. The receipt binds the exact
rule checksum, definition/input/expected/output hashes, lifecycle state,
accountable actor and time, evaluator contract, and current clean application
commit. Publication accepts only the latest passing receipt for the current
definition and current executor. Test cases are immutable and cannot be
deleted once the version leaves `Test`.

Configure the online-shadow contract before executing final Test-state cases:
the contract is part of the rule checksum and becomes immutable when the
version reaches `Historical Replay`. Any checksum change deliberately makes
older receipts stale.
