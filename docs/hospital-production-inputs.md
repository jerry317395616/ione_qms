# Hospital production inputs and sign-off register

IONE QMS deliberately does not invent clinical definitions, source-system
contracts, or legal approvals. This register is the minimum evidence required
before any corresponding integration, deterministic rule, indicator, alert,
or Agent is enabled.

## Source-system contract

Complete one signed row for every HIS, EMR, LIS, PACS, anesthesia, nursing,
medical-record, insurance, pharmacy, infection-control, transfusion, and
other in-scope endpoint.

| Required item | Acceptance evidence |
| --- | --- |
| System and data owner | Named business owner, DBA/API owner, security owner, escalation route |
| Environment and network | QA/production endpoints, allowlisted route, TLS/certificate ownership, timeout and maintenance window |
| Read-only authorization | Database/API grant proving application credentials cannot write source data |
| Record contract | View/resource name, field dictionary, types, null rules, code sets, sample payloads |
| Identity | Stable patient/encounter/document/result/operation keys and approved crosswalk rules |
| Incremental semantics | Modification timestamp, tie-break key, deletion/correction/version behavior, timezone |
| Reconciliation | Reviewed source count/hash query for `[period_start, period_end)`, expected filters, late-arrival window |
| Volume and SLA | Daily/peak volume, payload distribution, source latency, retention, outage and replay procedure |
| Privacy | Classification, minimum fields, masking, non-production handling, retention and deletion decision |
| Contract tests | Positive, empty, duplicate, correction, late, out-of-order, malformed, timeout and recovery cases |

The registered Oracle reconciliation query must return exactly one
non-negative `source_count` and may return a stable `source_hash`. A QMS-only
count is not source reconciliation.

## Clinical rule and indicator register

Every production version requires:

- exact policy/standard clause and effective dates;
- owner, author, independent reviewer, approver, and affected departments;
- trigger, population, exclusions, windows, severity, responsibility, due
  date, escalation, and allowed blocking behavior;
- field-level lineage to approved source contracts;
- positive, negative, excluded, missing, boundary, correction, and historical
  gold cases with expected results;
- replay and shadow reports, false-positive/false-negative disposition, waiver
  and appeal thresholds;
- numerator, denominator, precision, dimensions, target/baseline/direction,
  late-data policy, and manual-sample reconciliation for indicators;
- for every indicator, an approved mapping for each selected canonical
  dimension (`hospital`, `campus`, `department`, `ward`, `medical_group`,
  `physician`, `disease`, `surgery`, `drg`), including code-set ownership,
  missing-value behavior, enumeration scope, and source correction semantics;
- evidence that source-row manifests can be reproduced for the full approved
  retention window and that a late correction produces a new result revision
  without overwriting the prior revision;
- rollback/retirement criteria and signed residual-risk acceptance.

No Draft or Shadow blueprint is a clinical decision merely because the
software can execute it.

## Required topic packs

| Topic | Minimum source and signed clinical inputs |
| --- | --- |
| Running medical record | Document catalogue, required sections, creation/signature deadlines, diagnosis/order/result consistency, source locator |
| Terminal record/front page | Archive state, main/other diagnosis and operation coding, fee/order linkage, DRG/DIP grouping, coder/expert review |
| Surgery/anesthesia | Procedure dictionary and level, surgeon authorization, MDT/consent/checklist, anesthesia visit/monitoring/recovery/follow-up, cancellation/return/complication/death |
| VTE | Eligible population, bleeding/VTE assessment models, timing, exclusions, prevention matching, acknowledgement/escalation |
| Critical results | LIS/PACS catalogue, recipient and backup chain, notification/read-back/disposition timestamps, escalation |
| Medication/transfusion/nursing/infection | In-scope dictionaries, professional rules, ownership, exceptions and gold samples |
| Safety events | Taxonomy, severity, near-miss, anonymity/privacy model, investigation, root cause, measures, verification, sharing and non-punitive policy |
| 2026 ten goals | Approved per-goal source fields, rules, indicators, feedback workflow, baseline, target, owner and gold cases |

## AI and Qwen approval

Before enabling either AI switch or any Flow Trigger:

- Qwen is reachable only through an authenticated approved private endpoint;
- anonymous model discovery is rejected;
- exact model ID, streaming, tool calling, limits, timeout and failure behavior
  pass on the frozen build;
- embeddings/RAG remain disabled unless a separate Chinese retrieval,
  authorization, copyright, backup and restore acceptance passes;
- each of the five Agent categories has an approved Policy, minimal service
  user and tool set, complete ten-category synthetic evaluation set,
  independently approved signed threshold policy, current full evaluation
  receipt, independent review and release;
- prompt injection, cross-hospital/department/patient access, unconfirmed
  writes and sensitive output all have zero successful bypasses;
- the exact candidate proves rollback-only evaluation isolation (or uses an
  explicitly marked dedicated synthetic test site), real Flow/IONE tool
  execution, citation scope consistency, and zero retained evaluation
  fixtures/artifacts;
- named owners accept model limitations and incident/kill-switch procedures.

## Infrastructure and release acceptance

The hospital/operations owner must provide dated evidence for:

- SSO/MFA, TLS/WAF, secret management, log redaction, vulnerability scanning,
  penetration testing and privacy impact assessment;
- scheduler/worker health, queue alerts, on-call ownership and capacity;
- offsite database plus public/private file backup and an isolated restore;
- 1,000-user, 5-million-event/day and 10-million-rule/day equivalent load,
  latency, long-running and worker-recovery reports;
- 99.9% availability evidence and RPO at most 15 minutes/RTO at most two hours;
- frozen Frappe/Flow/IONE commit SHAs, Press App Release/Deploy Candidate,
  isolated QA install, double migration/build, role matrix, browser journeys,
  rollback and shared-bench smoke results.

Named hospital owners must sign these items. Software implementation, static
tests, or a successful installation cannot substitute for clinical, privacy,
security, operational, or management acceptance.

## Source-document locator activation

Before approving any source-document locator, the hospital must provide and
sign off:

- the exact EMR/source HTTPS origin and both Source System and Endpoint host
  allowlists;
- the exact source record type and static path prefix/suffix contract;
- an enabled read-only Endpoint with TLS verification and an approved
  hospital/campus/department/ward scope;
- existing browser SSO authorization behavior for every clinical role;
- a UAT case identifier proving the route exposes only the intended record;
- a UAT case identifier proving the route does not redirect across hosts; and
- positive, wrong-scope, missing-record, suspended-locator, and revoked-SSO
  tests.

Do not put credentials, tokens, real record identifiers, URLs with queries, or
document content in locator configuration or UAT-reference fields.

## Surgery governance activation

The hospital clinical and medical-affairs owners must approve and provide:

- the exact procedure dictionary, code-system version, procedure-to-Level
  I/II/III/IV matrix, effective periods and organization scope;
- the medical-staff and credential crosswalk and every surgeon authorization
  period, including evidence and independent approver;
- Level IV MDT applicability, minimum participant count, required role codes,
  conclusion semantics and evidence-reference contract;
- the exact pre-operative safety-check item codes and the meaning and source
  time of a passing result;
- required pre-anesthesia, intraoperative monitoring, recovery and follow-up
  stages and the postoperative completion window;
- cancellation, unplanned surgery, unplanned return, complication and
  mortality code sets, source timestamps and correction semantics;
- whether emergency exceptions are permitted at all, their allowed individual
  scopes, maximum duration, reason codes and accountable reviewer matrix; and
- whether governed-definition withdrawal can ever be retrospective and, if
  so, the approved `invalid_from` semantics, source lineage, migration,
  correction, reporting and acceptance cases; and
- positive, expired, wrong-scope, overlapping-period, missing-prerequisite,
  source-correction and concurrency test cases.

No catalogue, surgery level, participant threshold, checklist, outcome
classification, follow-up window or exception is enabled from a software
default. Missing or ambiguous approved inputs block pre-operative release.
Source-confirmed occurrence remains materializable as `Noncompliant` or
`Blocked`; it is not silently discarded. Retrospective withdrawal remains
fail-closed until the hospital supplies the explicit contract above.

## Quality meeting and experience-sharing activation

Before the meeting/action/experience domain is accepted for production, the
hospital quality-management and medical-affairs owners must approve and provide:

- the controlled meeting-type taxonomy and the scope in which each type may be
  used;
- committee and required-attendee composition, quorum rules, notice calendar,
  cancellation rules, and evidence-retention requirements for each applicable
  meeting type;
- named meeting-approver, minute-approver, action-owner/verifier, and
  experience-publisher mappings, including deputies, role eligibility, exact
  organization scope, and separation-of-duties rules;
- the approved agenda/material and attendance-evidence reference contracts,
  retention periods, access rules, and outage/manual-continuity procedure;
- the action due-date and escalation policy, overdue SLA, notification
  recipients, delegation behavior, closure-effect criteria, and cancellation
  authority;
- the experience-share content dictionary, de-identification method and
  forbidden-identifier dictionary, independent privacy reviewer, publication
  effective-period policy, retirement/version policy, and incident response;
- explicit confirmation that an AI report draft is meeting material only and
  cannot approve, finalize, or publish any governed record; and
- positive and negative UAT for each transition, creator/reviewer conflict,
  wrong scope, missing approver, frozen agenda mutation, duplicate concurrent
  approval/publication, incomplete attendance, absent evidence, overdue action,
  identifier leakage, native export/print denial, and rollback.

The software intentionally has no default committee, quorum, notice period,
required meeting type, escalation recipient, or de-identification dictionary.
These omissions are activation blockers, not values to infer from sample data.
