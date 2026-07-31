# Surgery authorization and perioperative governance

This feature implements the controls described in specification paragraphs
P112-P115 and the Level IV surgery component of national quality target seven.
Pre-operative release is fail-closed: no procedure catalogue, level,
authorization, MDT threshold, checklist item, care-stage requirement, or
emergency waiver is inferred by the application. A source-confirmed occurred
or finalized surgery is never discarded because a gate was breached; it is
retained as a noncompliant or blocked fact.

## Governed definitions

- `IONE Staff Qualification` and `IONE Surgery Authorization` use the strict
  `Draft -> Approved -> Retired` lifecycle. Approval requires a named
  independent reviewer, date/time, comment, frozen checksum, explicit validity
  period, and exact hospital/campus/department scope.
- `IONE Surgery Procedure Policy` is the hospital-approved procedure catalogue
  and level matrix. It freezes the exact procedure code and level, effective
  period, required checklist items, required perioperative care stages, Level
  IV MDT roles and participant minimum, postoperative follow-up window, and any
  allowed emergency-exception scopes and duration.
- Approval is serialized by logical definition slot and rejects overlapping
  effective periods. Approved fields and checksums are immutable. Records
  cannot be hard-deleted.

The hospital must create and independently approve these definitions. The app
ships no clinical catalogue, level mapping, MDT participant minimum, safety
checklist, or emergency default.

## Source event contract

Normalized surgery events must include:

- an exact `procedure_code`, `surgery_level`, operating `surgeon`,
  `surgery_time`, and explicit `surgery_phase`;
- normalized hospital, campus, and department scope on the linked immutable
  clinical event;
- source system, source record type, source record ID and source version;
- `source_finalized_at` for occurred, postoperative-finalized, and cancelled
  records;
- structured states and timestamps for pre-anesthesia assessment,
  intraoperative monitoring, recovery and postoperative follow-up when the
  source recorded them;
- explicit cancellation, unplanned surgery, unplanned return, complication,
  and mortality states, with the required reason/code/time evidence.

Known event namespaces map to a fixed phase. An unknown `Surgery.*` event must
declare a valid phase; an event/phase mismatch is rejected. Missing factual
care/outcome states are retained as `Pending`/`Not Observed` together with
stable missing-evidence reason codes; they are never treated as an implied
negative or compliant result.

The materializer stores only a hash of the source record identifier plus a
canonical clinical snapshot hash. The source event remains the detailed source
lineage. Before release, the read-only
`ione_qms.api.surgery.check_surgery_preoperative_release` endpoint requires
exactly one approved policy and one matching live primary-surgeon
authorization. After the source confirms occurrence,
missing/ambiguous/invalid policy evidence produces `Blocked`; authorization,
qualification, MDT, checklist, or required-care-stage failures produce
`Noncompliant`. Both outcomes are materialized read-only with `status=Failed`,
`compliant=0`, stable `governance_reason_codes_json`, and a governance checksum.

## Level IV MDT and safety checklist

Level IV policy approval is rejected unless it explicitly enables MDT and
defines a positive participant minimum and required role set. A completed MDT:

- is recorded before surgery through the governed POST API;
- contains exact named participants, roles, attendance confirmation times,
  conclusion, chair and evidence references;
- is checked against active staff records and the surgery scope;
- cannot authorize surgery when its conclusion is `Do Not Proceed`; and
- becomes immutable and checksum-protected with its participant manifest.

Every policy must define the exact pre-operative safety checklist item codes.
The completed checklist must cover that set exactly and every item must be
`Pass`. It is immutable and must predate the surgery.

## Emergency exception

Emergency exception support is disabled in policy unless the hospital
explicitly approves:

- the allowed subset of `Authorization`, `MDT`, and `Safety Checklist`;
- a positive maximum validity duration; and
- the affected procedure, level, scope and effective period.

A named clinical user requests an exact scope, reason code, narrative and
validity interval ending no later than the scheduled surgery. A different named
QC Reviewer or Medical Affairs user must approve it while the locked surgery is
still Scheduled and before its occurrence time. Historical and delayed events
require `reviewed_at <= surgery_time`, including an exception later expired
from Approved. The approved checksum, reviewer and timestamps are immutable.
An exception never waives the approved procedure catalogue or level mapping.
It can permit a specifically scoped pre-operative release, but the factual QC
projection records `EMERGENCY_EXCEPTION_USED` and does not infer compliance
from it.

## Perioperative and outcome record

`IONE Surgery QC` freezes:

- source identity hash, source version, finalization marker and snapshot hash;
- procedure, level, surgeon, organization and occurrence time;
- policy, authorization, qualification, MDT, checklist and exception lineage;
- pre-anesthesia, intraoperative monitoring, recovery and follow-up states and
  timestamps; and
- cancellation, unplanned surgery, unplanned return, complication and
  mortality states and their reason/code/time evidence.

The governance result and every linked evidence checksum are bound by
`governance_checksum`. Postoperative finalization additionally verifies every
care stage required by policy and the approved follow-up time window. It reuses
and verifies the occurrence-time frozen links/checksums, so later retirement of
a policy, authorization, or qualification cannot rewrite historical evidence.
For a delayed first event, historical eligibility requires `approved_at` and
the effective interval to cover `surgery_time`; a later `retired_at` does not
make the definition invalid at that earlier time.

## Permissions and operator UX

All lifecycle and evidence mutations are POST-only service methods. Desk forms
load `public/js/surgery_governance.js`, which provides role- and state-aware
approval, retirement, MDT, checklist, and emergency-exception dialogs and
reloads the authoritative document after each call. Query and document
permissions enforce clinical scope; immutable audit records do not grant
create, write, delete, import, native export, or print.

The release-check endpoint is GET and read-only. It returns a checksum-bound
decision receipt only on success and throws for a missing, ambiguous, late, or
invalid prerequisite. It is not yet wired to the hospital HIS/OIS/theatre
release action. Production activation therefore requires the hospital workflow
to call it immediately before release, reject timeouts and every non-success
response, and retain the returned receipt. Until that integration and its
failure-mode UAT are evidenced, this repository cannot itself stop a surgery
from proceeding.

## Production activation evidence

Before activation, retain signed evidence for the procedure/level catalogue,
staff and credential crosswalk, authorization matrix, MDT roles/minimum,
checklist codes, perioperative stage definitions, outcome code sets,
postoperative window, permitted emergency scopes, source field lineage, test
cases and responsible approvers. Bench migration, role-negative tests,
concurrent approval tests and source-event replay must pass on the immutable
release SHA. Until then, pre-operative release remains intentionally
fail-closed and the capability remains runtime-unverified.

The current governed-definition model has no retrospective `invalid_from`
field. A hospital retrospective withdrawal therefore cannot be represented by
ordinary retirement and must remain fail-closed until the hospital supplies
approved withdrawal semantics, source lineage, migration rules, and acceptance
cases. The application does not guess that a later retirement was retroactive.
