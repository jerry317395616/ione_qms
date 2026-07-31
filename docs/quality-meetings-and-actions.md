# Quality meetings, decisions, actions, and experience sharing

This domain implements the governed meeting loop required by section 6.2 of
the construction plan. It provides workflow and audit controls; it deliberately
does not invent a hospital's committee composition, quorum, notice period,
meeting-type catalogue, approval assignments, or de-identification policy.
Missing named actors or missing clinical scope fail closed.

## Governed records

| Record | Purpose | Creation and mutation boundary |
| --- | --- | --- |
| `IONE Quality Meeting` | Scoped meeting plan, frozen agenda, planned attendance, held evidence, and closure receipt | Draft content is authored in Desk; every state change uses the governed POST service |
| `IONE Meeting Agenda Item` | Ordered topic, objective, presenter, duration, and optional governed material reference | Child of the meeting; frozen when the meeting is approved |
| `IONE Meeting Attendee` | Planned participant and recorded attendance evidence | Child of the meeting; the planned manifest is frozen when approved |
| `IONE Meeting Minute` | Separate structured minute with discussion and proposed decisions | Draft content is authored separately; submission and review use governed POST services |
| `IONE Meeting Decision` | Immutable decision derived from an approved minute | Server materializer only; users have no generic create, write, delete, export, or print permission |
| `IONE Quality Action Item` | Accountable follow-up linked to one or more governed sources | Server service only; users have no generic create, write, delete, export, or print permission |
| `IONE Quality Action Verification Round` | Immutable owner-submission and independent-verifier receipt for one action round | Server materializer only; append-only, scope-filtered, and excluded from native export and print |
| `IONE Quality Experience Share` | Versioned, independently reviewed, de-identified experience | Draft content is authored in Desk; submission, publication, rejection, and retirement use governed POST services |

All parent records are clinical-scope filtered. Native Frappe export and print
are disabled, deletion is denied, and mutations retain Frappe version/audit
history. Controlled release evidence must use the application's approved export
flow instead of native list export.

## Meeting lifecycle

The only accepted progression is:

```text
Draft -> Submitted -> Approved -> Held -> Minutes Pending -> Closed
  \          \            \          \
   +----------+------------+-----------+-> Cancelled
```

- Submission requires a non-empty agenda, attendee plan, scheduled interval,
  hospital scope, chair, and two explicitly named approvers.
- The creator cannot approve the meeting. The configured meeting approver must
  be active, have an allowed approval role, and have exact scope access.
- Approval stores a canonical checksum of the agenda, material references, and
  attendee plan. Later mutation of that plan is rejected.
- Marking a meeting `Held` requires an actual start/end interval, an attending
  chair, an explicit `Attended` or `Absent` result for every planned attendee,
  per-row evidence or absence reason, and meeting-level held evidence.
- A meeting moves to `Minutes Pending` only after it is held. It closes only
  when exactly one linked minute has been independently approved.
- Cancellation requires a reason and is not available after closure.

An agenda may reference an `IONE AI Report Draft` only as meeting material.
The report draft must already be human-approved, match the meeting scope, and
have its source/content checksum bound into the frozen agenda. No model output
can approve a meeting, minute, decision, action, or publication, and the service
does not copy report text into an official record.

## Minutes and immutable decisions

Minutes use `Draft -> Submitted -> Approved` or
`Draft -> Submitted -> Rejected`.

- The minute creator cannot review it, and only the meeting's configured minute
  approver can decide it.
- Summary, discussion, evidence reference, and either structured decisions or
  an explicit no-decision reason are required.
- Approval takes a meeting-level database/advisory lock, revalidates the linked
  record, writes a unique approved-meeting key, seals the minute checksum, and
  derives decision rows in the same transaction.
- Each derived decision has a deterministic key, source minute, sequence,
  rationale, exact inherited scope, derivation receipt, and checksum.
- Approved minutes and derived decisions are append-only. Changes require a new
  governed record; approval history is never rewritten.

## Action ownership and verification

An action can link to a meeting, approved meeting decision, confirmed finding,
PDCA project, or medical safety event. The server resolves the links, rejects
incompatible scope, and inherits the most specific authorized scope.

```text
Open -> In Progress -> Pending Verification -> Closed
  \          \                  \
   +----------+------------------+-> Cancelled
```

- Title, description, owner, independent verifier, due date, and at least one
  valid source are required.
- Creator, owner, and verifier separation is enforced. Named users must be
  active, role-eligible, and authorized for the action scope.
- The owner records completion evidence and effect result before requesting
  verification. Every submission carries a caller-generated opaque request key.
  The service takes an action-scoped advisory lock and makes exact retries
  idempotent; a reused key with different content is rejected.
- A pending submission binds its round number, owner, submission time, evidence
  hash, and canonical owner-submission checksum. The verifier may close the
  action, return it to `In Progress`, or cancel a pending submission. The
  verifier decision also requires an opaque request key and is idempotent under
  the same lock.
- Every verifier decision first appends an immutable
  `IONE Quality Action Verification Round`. The receipt snapshots the submitted
  evidence and effect result, submission and verifier identities/times, decision
  and comment, resulting action state, previous receipt checksum, and its own
  canonical checksum.
- Receipt round numbers are contiguous and each receipt after round one binds
  the preceding receipt checksum. The service validates the complete chain,
  current pending checksum, latest-receipt pointer, scope, and actor separation
  on every governed transition. A hard limit of 100 rounds prevents unbounded
  validation work and must be handled operationally before another retry.
- Rework updates only the action's current workflow state and latest-receipt
  pointer. The rejected submission and verifier evidence remain in the
  append-only receipt; a later owner submission starts the next round instead
  of replacing history. Cancelling while pending likewise creates a `Cancel`
  receipt. Cancellation before submission requires a reason but has no
  verification receipt.
- The action's legacy current-state evidence and verification fields are not an
  authoritative audit ledger. Release, reporting, and incident review must use
  the linked verification receipts and validate their checksum chain.
- The action workbench offers `My Owner`, `My Verifier`, and authorized
  oversight views, caps results at 200, and shows overdue state. Scheduled
  quality tasks notify the owner and verifier for overdue open actions.

## De-identified experience publication

An experience share can originate only from a `Confirmed` or `Closed` medical
safety event, or an `Approved` meeting minute. The source is retained as
internal provenance; the published structured content must not contain patient,
encounter, source-record, raw-document, contact, or direct-identifier metadata.

- Content has a fixed structured JSON contract and is scanned recursively for
  forbidden keys and common direct-identifier patterns.
- The author must state the approved de-identification method and attest that
  de-identification was performed.
- The author cannot review their own submission. The configured publisher must
  be active, role-eligible, independently authorized, and exact-scope capable.
- Publication takes an experience-code lock and creates a unique active slot,
  version key, checksum, publication actor/time, and effective interval.
- A new version links to `supersedes`; an active publication must be retired
  with a reason before another version can occupy that code's active slot.
- Retirement preserves the published checksum and history. Source narratives
  are never copied or auto-published.

Pattern checks reduce accidental disclosure but cannot replace the hospital's
approved de-identification policy, dictionary, independent review, and privacy
acceptance tests.

## Desk and API use

Desk buttons call POST-only methods for submission, approval/rejection, held
attendance, minute review, action verification, publication, and retirement.
Action submission, review, and cancellation requests include a fresh opaque
idempotency key; clients may retry only the exact same operation with that key.
The `IONE Quality Action Workbench` Page uses the same POST-only scoped query.
Direct `frappe.db.set_value`, generic REST writes, native export, and manual SQL
are not supported business paths.

See [API summary](api.md), [clinical configuration
governance](clinical-governance.md), and [hospital production
inputs](hospital-production-inputs.md). Before release, run metadata/static
tests and repeat the lifecycle, concurrency, permission-negative, scheduler,
browser, migration, export-denial, and rollback tests on the pinned Bench/Press
candidate.
