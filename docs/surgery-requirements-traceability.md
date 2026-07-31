# Surgery governance requirements traceability

| Source requirement | Implemented evidence | Verification state | External production evidence |
| --- | --- | --- | --- |
| P112-P113: staff qualification, procedure level and surgeon authorization | `IONE Staff Qualification`, `IONE Surgery Authorization`, `IONE Surgery Procedure Policy`; explicit fail-closed `check_surgery_preoperative_release` API plus occurred/finalized `Noncompliant`/`Blocked` fact capture with stable reason codes and frozen evidence reuse in `services/surgery_governance.py` | Repository decision API implemented; hospital release action not connected; Bench-unverified | Approved procedure catalogue/level matrix, credential crosswalk, authorization evidence, HIS/OIS/theatre hard-block integration and failure-mode UAT |
| P114: pre-anesthesia visit, intraoperative monitoring, recovery and postoperative follow-up | Structured `IONE Surgery QC` stage states/timestamps; policy-required stage set and follow-up window; immutable source/snapshot/evaluation hashes | Implemented; static-verified; Bench-unverified | Approved source-field lineage, stage semantics, completion window, correction behavior and gold cases |
| P115: cancellation, unplanned operation, return to theatre, complication and mortality | Structured statuses, reason/code/time fields and source finalization/snapshot anchors on `IONE Surgery QC` | Implemented; static-verified; Bench-unverified | Hospital code sets, observation windows, reconciliation queries and clinical acceptance |
| National target seven: Level IV pre-operative MDT/discussion and safety check | `IONE Surgery MDT Record`, `IONE Surgery MDT Participant`, `IONE Surgery Safety Checklist`; exact approved role/minimum/item checks before occurrence | Implemented; static-verified; Bench-unverified | Approved MDT participant minimum/roles, conclusion semantics, checklist catalogue and UAT cases |
| Emergency clinical handling without implicit waiver | `IONE Surgery Emergency Exception`; policy-disabled by default, exact allowed scopes/duration, requester-reviewer separation and immutable checksum; exception use is a factual noncompliance reason and never proves compliance | Implemented; static-verified; Bench-unverified | Hospital decision to enable, allowed scopes, duration, reason codes, approval matrix and residual-risk acceptance |
| Operator workflow and least privilege | POST-only `api/surgery.py`; role/state Desk actions in `public/js/surgery_governance.js`; scoped query/document permissions; delete/native-export guards | Implemented; static-verified; Bench-unverified | Named-role negative tests, SSO/UAT, audit/export review and operational acceptance |

These rows establish repository coverage only. They do not assert that the
hospital clinical inputs exist or that the release has passed Bench, Press, or
production acceptance. See [surgery-governance.md](surgery-governance.md) for
the runtime contract and fail-closed behavior. The read-only release-check API
does not itself stop a hospital workflow; the hospital must connect it as a
fail-closed hard gate and retain its receipt. Retrospective withdrawal remains
an explicit external-input gap because the current model has no approved
`invalid_from` semantics.
