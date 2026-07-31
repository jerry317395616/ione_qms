# Security and privacy controls

## Access model

Frappe DocPerm is the coarse gate. IONE permission query conditions and
document permission hooks add hospital, campus, department, staff, owner, and
AI-task scope. `System Manager` alone does not grant access to sensitive
patient data. Only `Administrator` is a break-glass technical identity, and
its use must be monitored and excluded from routine business operation.

Primary roles:

- IONE QC Administrator
- IONE QC Reviewer
- IONE Medical Affairs
- IONE Department Director
- IONE Department QC Officer
- IONE Physician
- IONE Nursing/Pharmacy/IC QC
- IONE Agent Administrator
- IONE Agent Reviewer
- IONE Integration Administrator
- IONE Integration Operator
- IONE QMS Auditor
- IONE Agent Service
- IONE PHI Identity Reader

User Permissions and `IONE Medical Staff` assignments must be provisioned
before user acceptance testing. Negative cross-department and cross-patient
tests are mandatory for every role.

## Governed patient identity access

Direct patient and encounter identifiers are high-permission masked fields and
are absent from normal titles and search fields. No business role receives
generic field-level access. A named user must hold both `IONE PHI Identity
Reader` and a policy-authorized business role, retain clinical scope authority,
and use the purpose-bound POST disclosure path. The path requires an
independently approved, effective hospital policy and writes a hash-only,
append-only receipt before returning the minimum requested fields. No policy
or reader assignment is seeded, so a new installation fails closed. See
`docs/privacy-identity-access.md` for the policy lifecycle, audit contract, and
production activation gate.

## Integration security

- Incoming endpoints are disabled until an owner, source, direction, network
  scope, and credential are approved.
- Source and Endpoint tenant rows are hierarchical, explicit, audited, and
  fail closed. Endpoint authority can never exceed Source System authority.
- A named Integration Administrator owns configuration. Integration Operators
  cannot broaden tenant scope, rotate signing identity, or enable endpoints.
- Payloads are capped at 2 MiB at the request boundary.
- HMAC-SHA256 covers timestamp, nonce, endpoint identity, and the exact raw JSON body.
- The clock window is five minutes; nonces are cached for ten minutes.
- Source CIDRs are explicitly allowlisted.
- Endpoint secrets use Frappe Password encryption and are never committed,
  logged, returned, or placed in URLs.
- Mapping targets and nested source paths are allowlisted. Expressions,
  arbitrary Python, raw SQL, and dynamic import paths are rejected.
- Invalid-schema, oversized, tenant-unresolved, and out-of-scope input retains
  only a bounded hash manifest; rejected raw PHI and raw source identifiers are
  not written to Messages, quarantine records, or logs.
- Pull connectors are code-reviewed registrations and source accounts must
  have database/API read-only grants. QMS never sends `UPDATE` or `DELETE` to
  a clinical source.

## AI security

- Patient data may be sent only to a model host explicitly listed in
  `IONE AI Settings.allowed_model_hosts` and using an internal IP or
  `.internal` hostname.
- Production readiness requires endpoint authentication. Anonymous model
  discovery must return 401 or 403.
- Each IONE Flow Agent has an explicit domain-tool set. Broad Flow tools
  `create`, `update`, `delete`, `run_action`, and `execute` are forbidden.
- `update_memory` is not permitted because it has no approved medical-data
  retention boundary.
- Artifact-creating tools require confirmation. A policy with such a tool
  cannot enable auto approval.
- Trigger users must be dedicated non-Administrator service users with the
  `IONE Agent Service` role.
- AI Analysis Tasks can be inserted only while the checked creation API holds
  an exact in-process capability. Document hooks deny generic Desk/import/REST
  creation even to Frappe's technical `Administrator`, freeze requester/scope/
  input provenance, and require the input SHA-256 again before the first claim.
- A terminal scheduled report task is preserved. Only its exact configured,
  independent report reviewer can create an immutable, checksum-bound recovery
  receipt through POST; the derived replacement task keeps prior-task/receipt
  provenance. Two replacements per approved schedule/month is a hard limit,
  after which a sanitized incident is opened and month progression fails
  closed.
- Agent Test Cases use a versioned typed contract and must cover all six
  quality and four security categories. Output-length-only, missing-category,
  missing-evidence, invented-citation, and tool-name-only suites cannot pass.
- Model-requested tools execute only through the live Flow schema and ordinary
  IONE policy, argument, scope, and evidence guards in a commit-blocked,
  savepoint-rolled-back synthetic tenant/session. Any isolation uncertainty
  fails closed and requires an explicitly marked dedicated test site.
- Evaluation receipts retain no raw model/fixture content and bind every
  release/model/prompt/instruction/tool-schema/policy/KB/suite/threshold/code
  input. Production has zero tolerance for cross-scope overreach, forbidden
  tools, PII canary leakage, and missing or scope-inconsistent evidence.
- Every patient-data tool read writes an `IONE AI Data Access Log` containing
  scope, purpose, fields, actor, task, request hash, source lineage, integrity
  hashes, and a bounded evidence snapshot. The snapshot is classified input
  data and is removed at the input-retention boundary; the receipt and hashes
  remain.
- Initial and Resume calls commit an opaque-token claim before crossing the
  model boundary and finalize only through attempt/token compare-and-swap.
- Flow runs are linked to policy, agent, model, tools, input/output hashes,
  usage, and status through append-only, per-iteration revisions. Complete
  transcript, tool-trace, and question integrity hashes are retained. Governed
  IONE Flow runs prohibit session attachments; the revision records the
  canonical empty-attachment hash, and any attachment fails closed.
- Model output creates drafts or candidate findings only. Human acceptance
  creates a formal finding with an immutable link to the candidate.
- Scheduled aggregate-report output is scanned again outside all model tools.
  The outer gate covers final output, questions, errors, tool arguments,
  assistant/tool transcript content and provider-controlled tool-call IDs on
  initial, resumed and orphaned runs. A match or scanner failure commits a
  content-free quarantine before later audit writes, clears those Flow
  surfaces, deletes only current-attempt unreviewed drafts, and records a
  checksum-bound contained privacy incident. This prevents final-answer,
  malformed-tool and unknown-tool paths from bypassing draft validation.

If any target-bench app overrides Flow approval APIs, the effective hook chain
must be captured for the exact candidate and proven unable to bypass IONE
write confirmation. This includes, but is not limited to, `ione_core`. The
safest operational baseline is “always confirm” globally, plus
application-level denial for high-risk IONE operations.

## Data minimization and logging

- Full prompts, source payloads, record text, attachments, provider responses,
  and tool payloads must never be emitted to operational logs. Governed Flow
  persistence is classified application data and is retained only for the
  configured period before integrity-checked redaction.
- Passwords, bearer tokens, patient names, IDs, phone numbers, and addresses
  must never be written to operational logs.
- Operational logs retain request ID, object name where authorized, status,
  error category, duration, token count, model ID, and hashes.
- UI APIs return only the minimum fields needed by the screen.
- Dashboard aggregates contain no patient identifiers.
- Exports require a separate business role and approval; technical
  administration is not an export entitlement.

## Controlled export authorization

- Request creation canonicalizes the target DocType, filters, and fields, then
  derives and freezes `scope_mode`, hospital, campus, and department from the
  validated filter. Clients cannot supply or later alter the frozen scope.
- A single exact department or campus filter is a scoped export. Hospital-wide,
  multi-scope, and unscoped requests require an independent named Medical
  Affairs approver.
- An Auditor can list, read, approve, reject, or download only a department or
  campus request covered by an explicit Frappe User Permission. Parent hospital
  and campus grants may cover their child scope, but never authorize a
  hospital-wide export.
- The requester can read and download only their own request, and can never
  approve or reject it. `Administrator`, `Guest`, disabled users, and agent
  service identities are not business export actors.
- Approval, rejection, generation, and expiry use the same per-request
  advisory lock and a database row lock. The first terminal decision wins.
- A generated CSV is private, attached to exactly one completed request, and
  is downloadable only while that request is unexpired and the reader still
  passes the parent permission check. File mutation and sharing are denied.
- Frappe's native Data Export endpoint is denied for the complete governed QMS
  DocType set, including for `Administrator`; governed data must use an
  approved `IONE Data Export Request`. Generator permissions and the runtime
  deny set are checked for exact equality, and every governed DocType has
  native `export` permission disabled.
- The endpoint guard recursively inspects `doctype` and `parent_doctype`
  values across JSON strings, mappings, lists, and tuples. Inspection has
  explicit depth, node, and string-size limits; malformed or uninspectable
  input that may name a QMS DocType fails closed. Valid non-QMS exports retain
  Frappe's upstream behavior and permission checks.

## Immutability and deletion

Evidence, QC executions, AI access logs, Flow run links, release records, and
workflow audit events are append-only. Retention jobs may redact designated
AI draft payload fields after policy expiry but may not delete clinical
evidence or audit history. Any legal deletion workflow must be separately
approved, dual-controlled, logged, and tested against retention obligations.

Retention redaction fails closed. It requires a terminal governed task, the
latest immutable Flow revision, and exact 64-character payload and artifact
hashes. Missing or mismatched evidence causes a sanitized skip; no irreversible
redaction occurs. Input retention covers user/tool messages, tool traces,
questions, bounded access-log evidence snapshots, and the proposed arguments
and prompt of terminal `Consumed`/`Expired` tool approvals. `Pending` and
`Reviewed` approvals are never redacted. Access snapshots require both their
content hash and original append-only record hash; approval fields require
independent hashes and an exact retention capability. Legacy approvals without
the prompt hash, partial markers, and any Flow session attachment fail closed
for controlled operational review. The retention job never claims to delete an
attachment binary. Output retention covers assistant messages even when the
Flow Run output field is empty. Lineage, revisions, hashes, approval provenance,
access receipts, and audit comments remain. Install/migrate raises, but never
lowers, Frappe `Log Settings` retention for `Flow Session` to at least the
maximum configured input/output retention plus seven days. The IONE Flow
Session controller extension preserves the official cleanup path for ordinary
Flow chat but places governed sessions on an indefinite deletion hold until
all terminal runs have complete redaction markers and immutable hash receipts.
Integrity failures and legacy attachments therefore cannot be bypassed by the
generic Flow log-cleanup deadline.

## Secrets and key management

- Store Qwen/API secrets in Press secret or encrypted site configuration and
  Flow Password fields.
- Before installing the app, configure four independent secrets of at least
  32 bytes and explicit key IDs: `ione_identity_hmac_key[_id]`,
  `ione_phi_hmac_key[_id]`, `ione_rule_receipt_hmac_key[_id]`, and
  `ione_sensitive_hash_hmac_key[_id]`. None may equal another or fall back to
  Frappe's general `encryption_key`; install and migrate fail before writes if
  this contract is missing.
- During PHI-key rotation, move the prior key into the bounded
  `ione_phi_hmac_verify_keys` verification-only ring before changing the active
  ID. Identity rotation instead requires a two-phase overlapping ring: first
  keep the old key active, add the future key to
  `ione_identity_hmac_verify_keys`, and restart the whole worker fleet; then
  atomically make the future key active, retain the old key, and restart the
  whole fleet again. Only then run the bounded identity migration to verify
  each old surrogate under its exact retained contract and atomically replace
  it under the active key. Require `has_more=false`, `blocked=0`, a subsequent
  zero-update pass, and zero non-active Patient/Encounter contracts before
  removing the old key. Rule replay/shadow receipt rotation likewise retains prior keys in
  `ione_rule_receipt_hmac_verify_keys`. All complete rings are validated for
  safe IDs, minimum length, and unique IDs/secrets. Keep every key required by
  an active policy, retained access receipt, rule receipt chain, or identity
  row until its governed migration is complete.
- Rotate credentials without changing code or Git history.
- Back up the Frappe encryption key separately from the database with access
  controls and recovery documentation; back up all four IONE keys and every
  retained verification/rotation ring under the same separately controlled
  recovery process.
- Never place credentials in Git remotes, CI output, deployment records,
  screenshots, issue trackers, or this repository.
- Use least-privilege GitHub App/deploy credentials scoped to the
  `ione_qms` repository for Press source access.
- Before every deployment, capture the target bench's complete
  `override_doctype_class` and `extend_doctype_class` chains for `File`. The
  effective MRO must place `IONEQMSFileMixin` before the selected Drive/core
  controller and preserve every cooperative permission/lifecycle hook.
  Multiple `File` overrides or a non-cooperative extension are release blockers
  until ordering and behavior are proven.

## Security release gate

Production is blocked until SAST, dependency review, secret scanning, role
matrix tests, cross-scope negative tests, integration replay tests, prompt
injection tests, Qwen authentication, audit-log verification, backup restore,
and rollback rehearsal all pass against the exact release commit.
