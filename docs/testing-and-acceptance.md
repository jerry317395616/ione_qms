# Testing and acceptance

## Automated suites

| Suite | Coverage |
| --- | --- |
| Unit | operators, rule trees, mappings, schemas, hashes, states, formulas |
| Frappe integration | DocTypes, indexes, hooks, permissions, jobs, idempotency |
| Clinical rule | positive/negative/missing/exclusion/boundary/regression samples |
| Integration | signing, replay, retry, ordering, reconciliation, source read-only |
| AI contract | model, streaming, tool calls, confirmation, scope, failure |
| Security | SAST, dependencies, secrets, roles, IDOR, SSRF, injection, audit |
| Performance | event/rule volume, user concurrency, dashboards, queue recovery |
| Recovery | backup restore, prior release rollback, message reconciliation |

The CI workflow is configured to run Ruff, compilation, schema validation,
unit/integration tests, manifest checks, dependency audit, SBOM generation, and
secret scanning on fixed runner/tool versions. Its clean-Bench matrix
materializes immutable source trees from the fork repositories before
`bench init`: the develop compatibility combination uses Frappe
`0a1eb8e38babeccfcd2e1845efa04b376a63366f` and Flow
`4ac02293f656d9a979214c62b168512b5b6d008e`; the `manager` snapshot
combination uses Frappe `5aef9956c7bf58d36cfd59e2242cc1a26d385ea2`,
the same Flow SHA, and Drive
`cd3438d1ab0b0fc1b8c10e282639ec0bd2ee7d82`. It installs the complete
dependency combination, builds all assets, runs dependency and QMS tests,
proves every application checkout still has the expected clean HEAD, audits
the installed Bench Python environment, and emits a manifest plus CycloneDX
artifact for each matrix entry.

The ARM64 CI job only cross-resolves binary CPython 3.14 Linux wheels and
records their hashes. It detects an important class of packaging failure but
does not execute Frappe, Node/Rust assets, MariaDB, Redis, or workers on ARM64.
A production-like native ARM64 Press QA candidate must run the full install,
asset build, migrate, bounded post-migrate convergence/receipt verification,
Frappe/Flow/Drive/QMS tests, worker/scheduler checks, Qwen contract, browser
journeys, backup restore, and candidate rollback. Its image-level SBOM covers
Python, Node, operating-system packages, and exact app SHAs. No suite is
considered passed without logs tied to the exact release and Press candidate.

The clean-site CI lifecycle executes the bounded migration worker and then a
separate `assert_migration_complete` command under `set -e`. Returning a
Pending/Blocked result from the worker is not sufficient: a missing,
hash-invalid, old-version, or old-fingerprint receipt makes the job non-zero.
Fingerprint tests cover CRLF normalization, canonical DocType JSON, Python
content changes, migration-plan changes, app-version changes, and receipt
tampering.

## Required role matrix tests

For each DocType and endpoint, test read, list, create, write, submit, cancel,
delete, export, print, report, and API access for every applicable role.
Include negative access to another hospital, campus, department, staff member,
patient, encounter, finding, indicator detail, AI task, and Flow Agent.
Confirm that System Manager without IONE clinical roles cannot read sensitive
records.

Run the File controller suite both with and without Drive installed. In the
Drive case, the effective MRO must include the QMS cooperative mixin before the
Drive controller. Verify team upload/rename/move/share/download/delete and
cleanup, ordinary site files, QMS evidence mutation/optimize/trash/download
denial, and controller cache reload across sites. A Drive team File must not be
accepted as `/private/files/` appeal evidence.

For controlled exports, create at least two hospitals, two campuses, and two
departments with separate Auditor User Permissions. Prove that list, direct
document read, approval, rejection, and private-file download all deny a
cross-scope Auditor. Prove that hospital-wide, multi-scope, and unscoped
requests are visible and actionable only to Medical Affairs, that the
requester cannot decide their own request, and that concurrent approve/reject
calls produce one decision and one generation job. Expired and mismatched
attachments must return no file content.

## Functional acceptance

- Standards and clauses retain source/version/effective/approval lineage.
- Rules reproduce findings with version and evidence.
- Online, terminal-record, record-front-page, surgery/anesthesia, and safety
  workflows create traceable quality records.
- Findings complete confirmation, appeal, rectification, department approval,
  functional verification, closure/rework, and audit.
- Indicators reproduce numerator, denominator, value, dimension, and details.
- Integration retry does not duplicate messages, events, executions, or
  findings.
- HL7 v2 ingress rejects invalid signatures/replays, media types/charsets,
  oversized or over-count batches, malformed ER7/MLLP framing, and parses the
  complete batch before creating any clinical receipt. Accepted test cases
  preserve MSH-10/MSH-13 idempotency and frozen Mapping lineage without
  inferring clinical scope from segment values.
- Dashboard and workbench respect scope and link to underlying evidence.
- AI calls record requester, policy, service user, agent, model, tools, data
  access, hashes, output status, reviewer, and adoption.

AI execution acceptance must inject and verify all of these boundaries:

- two workers attempting the same initial or Resume claim;
- a run exceeding the nominal lease while its original RQ job remains active;
- a crash after Flow returns but before task compare-and-swap finalization;
- an approval crash after review/before enqueue and after resume/before commit;
- Resume lease expiry without automatic replay of an approved write tool;
- several terminal iterations of the same Flow Run producing immutable
  revisions selected in the correct order;
- exact orphan and abandoned-Running reconciliation;
- aggregate monthly output containing a phone, identity number, email, labeled
  record identifier, hidden control character, or external link in each of:
  no-tool final output, malformed/unknown tool arguments, provider-controlled
  tool-call IDs, questions, errors, initial messages, resumed messages and an
  orphaned terminal run; every case must leave only the fixed quarantine
  marker and a content-free incident;
- a crash or injected failure after scheduled report task insertion, during
  schedule runtime-pointer save, after enqueue-after-commit registration,
  during database commit, and during the post-commit Redis enqueue; no
  rolled-back task may execute, no month may advance, and the bounded Pending
  sweep must recover only a genuinely committed task with the same job ID;
- rollback of every business mutation produced by a superseded worker;
- no Flow redaction while its governed task is non-terminal, including an
  already-completed Flow run awaiting orphan reconciliation;
- hash-verified access-snapshot and terminal-approval redaction, with negative
  cases for pending/resumable approvals, missing legacy prompt hashes, partial
  markers, altered access receipts, and governed Flow attachments;
- generic Frappe/Flow log cleanup deletes an aged ordinary session, deletes a
  governed session only after complete retention verification, and indefinitely
  preserves governed sessions with missing links, partial markers, non-terminal
  tasks, attachments, or verification errors;
- every Agent suite contains every governed typed category and valid synthetic
  scope fixtures, structured output/evidence/citation expectations, closed
  allowed-tool argument schemas, and expected denials;
- an allowed IONE tool name with hostile cross-hospital/department/patient
  arguments is denied by the real scope path; an unknown/forbidden tool, a
  nonexistent citation, a missing citation, and a synthetic PII canary each
  produce a failed case and zero production authorization;
- model configuration, evaluation prompt, Agent instructions, live tool
  schema, policy, KB source/sync manifest, suite, threshold policy, and
  evaluator-code mutation each make an old pass unusable;
- concurrent suite/release mutation cannot cross the row/advisory locks, and a
  newer failed or pending evaluation blocks an older pass;
- each case leaves no fixture, task, Flow session/run, access log, draft,
  candidate, formal finding, action, rectification, verification, or PDCA row
  after its mandatory rollback.

## Performance acceptance

Use representative distributions and clinical payload sizes. Record hardware,
app commits, database volume, concurrency, duration, warm-up, percentiles,
errors, queues, model parameters, and resource graphs.

The guarded k6 workloads and evidence procedure are in
`tests/performance/README.md`. They refuse production targets by default and
must run only against an approved isolated QA/performance site with synthetic
data.

Pass gates:

- at least 1,000 concurrent users at agreed workflow mix;
- at least 5 million events/day equivalent throughput;
- at least 10 million deterministic rule evaluations/day equivalent;
- realtime rule p95 at most 3 seconds;
- normal page p95 at most 2 seconds;
- no duplicates or lost events during worker restart;
- model capacity and graceful-failure thresholds meet approved AI use cases;
- availability, RPO, and RTO targets demonstrated.

## Clinical and AI sign-off

The hospital supplies approved source inventory, mappings, clinical
definitions, exclusions, thresholds, responsibility rules, sample cases, and
named approvers. Test fixtures must be synthetic or properly de-identified.
No production rule or AI workflow is enabled solely because software tests
pass.

## Go/no-go

Any open P0, failed backup restore, failed rollback, cross-scope access,
anonymous Qwen access, unreviewed high-risk AI path, critical rule false
negative, unresolved data reconciliation, or unpinned application commit is a
no-go.
