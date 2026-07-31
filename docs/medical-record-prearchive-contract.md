# Medical-record pre-archive control contract

This contract governs the local IONE projection and the decision exchange with a
hospital source system. IONE never changes the source system directly.

## Frozen policy inputs

An approved sampling policy must contain:

- one exact `source_system`; a policy cannot assert completeness across a
  guessed or implicit multi-source population;
- a canonical, sorted `mandatory_rule_manifest_json` with one or more exact
  `{rule, rule_version, rule_checksum}` identities;
- the generated `mandatory_rule_manifest_hash`;
- an explicit `auto_allow_deterministic_pass` value;
- the existing coder and expert sample parameters and archive outcome mappings;
- an independently approved policy checksum and a configured sampling key.

IONE does not ship placeholder clinical rule IDs. The hospital must supply and
approve the mandatory rule versions and their clinical semantics.

## Source completeness watermark

Batch creation requires an immutable
`IONE Medical Record Completeness Watermark` created only by the governed
integration service. A local poll time, the newest record observed, or a
human-entered date is not a completeness watermark.

The signed source assertion is bound to one endpoint, one source system, one
exact hospital/campus/department/ward scope, one strictly increasing
`source_sequence`, one exact population period and predicate, and one
`complete_through` date. It freezes the source-provided watermark ID and record
count, the canonical source manifest hash, the exact signed request hash, the
source assertion and receipt timestamps, and a `watermark_checksum` over those
immutable semantic and provenance fields. The source assertion time, normalized
to the site timezone, must be at or after the next local midnight following
`complete_through`. Integration operators, integration administrators,
authorized QC reviewers, Medical Affairs, and auditors have read-only,
scope-filtered access; generic Desk, import, REST, and database-save creation is
not an approved path.

The source sends the assertion through the POST-only, signed inbound endpoint
as canonical JSON with exactly:

```json
{"campus":"","complete_through":"2026-06-30","department":"","eligible_record_status":"Final","hospital":"<approved hospital>","period_end":"2026-06-30","period_start":"2026-06-01","population_date_field":"source_finalized_at","source_asserted_at":"<ISO timestamp with offset at/after 2026-07-01T00:00 site time>","source_manifest_hash":"<sha256>","source_record_count":0,"source_sequence":1,"source_watermark_id":"<16-128 safe characters>","ward":""}
```

For every new Initial or Supplemental batch, IONE reconciles that assertion
against the entire current eligible population, before subtracting records
already covered by an earlier batch. For each eligible record it computes:

```text
leaf = sha256(canonical_json({
  "record_snapshot_hash": "<sha256>",
  "source_record_id_hash": "<sha256>",
  "source_record_type": "<exact source type>",
  "source_finalized_at": "<site-local ISO datetime with six fractional digits>"
}))
source_manifest_hash = sha256(canonical_json({
  "eligible_record_status": "Final",
  "period_end": "YYYY-MM-DD",
  "period_start": "YYYY-MM-DD",
  "population_date_field": "source_finalized_at",
  "source_identity_leaves": sort(all_leaf_values)
}))
source_record_count = len(all_leaf_values)
```

Canonical JSON uses UTF-8, sorted keys, no insignificant whitespace, and the
leaf list is lexicographically sorted. Duplicate source identities or leaves
reject the batch.
Local document names, patient identifiers, encounters, and narratives are not
part of this source reconciliation identity. Both the count and hash must
exactly equal the signed watermark; an early empty assertion, a dropped local
record, or an obsolete late-arrival watermark fails closed. The assertion time
must also be no earlier than the maximum `source_finalized_at` in the current
full eligible population. The batch key
binds the watermark checksum plus its source count, source manifest hash, and
cutoff, so the linked immutable watermark supplies the frozen audit evidence
without duplicating those fields on the batch.

Each review batch freezes the watermark link, source system,
`source_complete_through`, and the watermark checksum as
`source_completeness_hash`. An `Initial` batch covers its declared period only
when the exact source assertion is complete through the period end. Watermark
receipt and batch creation serialize on the same source-system plus exact-scope
stream lock. While holding that lock through population selection, assignment
insertion, and commit, a batch may use only the current latest watermark.
Late source records use an explicitly linked `Supplemental` batch with a
positive, monotonic supplement sequence and a watermark whose source sequence
and assertion time are both strictly later than its parent; an earlier
population manifest is never silently rewritten.

## Full-population disposition

Batch creation freezes every eligible `Final` record in the period. Every record
receives an immutable assignment and an initial archive decision:

- the coder sample enters `Coder Review`;
- the expert sample is a subset of the coder sample;
- records outside the coder sample are `Closed`, but still receive a durable
  decision;
- missing events, missing executions, snapshot mismatch, conflicting exact
  execution receipts, `Failed`, `Excluded`, `Insufficient Data`, definition
  drift, and execution errors all produce `Hold`;
- automatic `Allow` is possible only outside the coder cohort, only when the
  approved policy explicitly permits it, and only when every exact mandatory
  deterministic version has exactly one completed `Passed` execution whose
  context hash equals the frozen record snapshot hash. Multiple exact receipts,
  even multiple `Passed` receipts, are an ambiguous conflict and produce
  `Hold`.

Human reviews and overrides append new decisions. They never rewrite the initial
decision or a prior review receipt. Every sampling-policy Activate, Suspend, and
Retire transition has an immutable, checksum-bound receipt linked to its
predecessor; the policy only mirrors the latest receipt. Under the shared record
release lock, an override walks every assignment on the record, every complete
supersession and linked review chain, every batch, every policy approval, and
the full operation history of every represented policy. Missing or broken
lineage fails closed. The override approver cannot be any historical coder,
expert, decision/review actor, batch creator, policy author, policy reviewer, or
policy operator anywhere on that record, including actors from another policy.
A delivered decision must first receive a signed `Rejected` or `Failed`
acknowledgement; an `Applied` decision is terminal.

The application intentionally does not claim to supply the hospital's final
dual-approval authority for a `Held` to `Allow` exception. Before production,
the hospital must place that transition behind an external, independently
staffed dual-review gate (for example, Medical Affairs plus Health Information
Management), retain both approvals and the approved reason code, and call this
override service only after that gate succeeds. A single IONE role assignment
is not evidence of hospital dual approval.

## Source-system pull and acknowledgement

The source calls the POST-only archive-decision pull endpoint through an enabled
`Inbound` HMAC endpoint. The exact canonical JSON request is signed with the
standard `X-IONE-Timestamp`, `X-IONE-Nonce`, `X-IONE-Endpoint`, and
`X-IONE-Signature` contract. Existing endpoint controls enforce clock skew,
nonce replay prevention, source CIDR allowlisting, rate limits, enabled source,
and allowed hospital scope.

The request body contains exactly:

```json
{"campus":"","department":"","hospital":"<approved hospital>","limit":50,"request_id":"<16-128 safe characters>","ward":""}
```

The response contains only governed decision identifiers, checksums, hashed
source identity, rule-gate reason codes, and acknowledgement state. It contains
no raw source record ID or patient narrative. A request ID is idempotent only
while its exact receipt remains current. Before recomputation, every stored
constituent with `ack_required=1` must still have its assignment pointing to
that exact delivery and exact stored envelope hash. Replay then recomputes the
authorized endpoint, scope, record envelopes, decision and envelope manifests,
counts, canonical response, and response hash. Changed content or authority, a
superseded decision, a completed acknowledgement that cleared pending linkage,
or a stale envelope rejects the replay. A named, enabled
`IONE Integration Operator` has a separate authenticated reconciliation API.

Every delivery freezes a bounded envelope manifest. Each assignment records its
pending delivery and exact envelope hash, and the source acknowledgement must
bind to that same delivery and envelope hash. The source applies the decision
under its own approved workflow and sends the existing exact signed
acknowledgement. Only an `Allow` decision with an exact-envelope `Applied`
acknowledgement can move the local projection from `Final` to `Archived`,
through the distinct in-process archive-application capability. Assignment-less
records and direct Desk/REST saves remain blocked.

The acknowledgement body contains exactly the current delivery linkage:

```json
{"ack_status":"Applied","archive_decision":"<sha256 decision name>","decision_checksum":"<sha256>","delivery":"<sha256 delivery name>","envelope_hash":"<sha256>","message_code":"ARCHIVE_APPLIED","source_ack_at":"<ISO timestamp with offset>","source_ack_id":"<source-unique ID>","source_record_id_hash":"<sha256>"}
```

An exact ACK replay is accepted only while that receipt remains the assignment's
current acknowledgement for its latest decision. A changed request ID cannot
acknowledge a cleared, superseded, or differently delivered envelope.
`Applied` is terminal for either decision value; a hospital workflow that may
seek an independently approved exception to a Closed `Hold` must acknowledge
that delivery as `Rejected` or `Failed` before requesting an override.

The delivery unit is one medical record, not one policy assignment. Its
effective decision is `Allow` only when every current assignment for that
record is `Closed` and its latest decision is `Allow`; every other combination
is a record-level `Hold`. A record with any `Coder Review` or `Expert Review`
assignment is not published and receives no pending delivery; absence from the
pull queue is the fail-closed source contract until all gates are Closed.
Closed `Hold` and Closed `Allow` envelopes are both publishable. Delivery
publication, review closure, override, acknowledgement, supplemental
assignment creation, and local archive application share the same record-first
release lock.

## Required external configuration before acceptance

- approved mandatory deterministic rule IDs, versions, checksums, clinical
  definitions, test evidence, and effective dates;
- a documented guarantee that the medical-record `record_snapshot_hash` equals
  the deterministic execution context hash;
- a read-only source-system identity and enabled signed endpoint with a
  32-character-or-longer HMAC secret, CIDR allowlist, source scopes, and rate
  limit;
- an approved source completeness assertion protocol, canonical source manifest
  definition, late-arrival policy, and supplemental-batch operating procedure;
- source-side pull, persistence, decision application, retry, acknowledgement,
  and reconciliation behavior;
- approved coder/expert sampling rates, allow/hold mappings, override reason
  catalogue, retention, and segregation-of-duties policy;
- hospital integration, privacy, clinical, security, and UAT sign-off.

Until these hospital-owned inputs and tests are complete, the archive gate must
remain fail-closed and this implementation must not be represented as clinically
accepted for production.
