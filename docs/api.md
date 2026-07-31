# API summary

All paths use Frappe authentication unless an integration endpoint performs
its own HMAC authentication. JSON errors follow Frappe response conventions.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/method/ione_qms.api.integration.receive_event` | Receive an idempotent clinical event |
| POST | `/api/method/ione_qms.api.integration.receive_hl7_v2` | Receive a signed, bounded HL7 v2 ER7 batch |
| POST | `/api/method/ione_qms.api.quality.evaluate_encounter` | Evaluate selected published rules |
| GET | `/api/method/ione_qms.api.quality.get_patient_alerts` | Read current authorized alerts |
| POST | `/api/method/ione_qms.api.rules.run_test_case` | Run one Test-state rule case and append an exact execution receipt |
| POST | `/api/method/ione_qms.api.rules.run_version_tests` | Run the bounded governed test suite for one Test-state version |
| POST | `/api/method/ione_qms.api.rules.start_validation_run` | Queue a bounded rollback-only `Historical Replay`; `Shadow Run` is live-event driven and is not an accepted `run_type` |
| POST | `/api/method/ione_qms.api.rules.archive_standard_source` | Archive one Draft Standard's allowlisted HTTPS `source_url` into an attached private File and return its SHA-256 |
| GET | `/api/method/ione_qms.api.rules.get_standard_version_difference` | Read one bounded, checksum-paginated Standard Version comparison |
| POST | `/api/method/ione_qms.api.improvement.submit_rectification` | Submit a rectification plan |
| GET | `/api/method/ione_qms.api.indicator.get_indicator_trend` | Read authorized pointer-selected current indicator revisions |
| GET | `/api/method/ione_qms.api.indicator.get_indicator_dimension_contract` | Read the non-secret canonical dimension/source capability contract |
| POST | `/api/method/ione_qms.api.indicator.request_historical_indicator_verification` | Request a purpose-, scope-, TTL-, row-, and use-bounded historical reproduction |
| POST | `/api/method/ione_qms.api.indicator.review_historical_indicator_verification` | Independently approve or reject an exact historical reproduction request |
| POST | `/api/method/ione_qms.api.indicator.verify_indicator_result` | Verify a current result or consume one approved historical-audit use and append an access receipt |
| POST | `/api/method/ione_qms.api.ai.create_analysis_task` | Create a governed AI task |
| POST | `/api/method/ione_qms.api.ai.review_agent_tool_call` | Review one exact paused tool call |
| POST | `/api/method/ione_qms.api.ai.resume_agent_tool_calls` | Queue a governed approval resume |
| POST | `/api/method/ione_qms.api.ai.review_agent_evaluation` | Accept/reject an Agent evaluation |
| POST | `/api/method/ione_qms.api.ai.review_candidate_finding` | Accept/reject an AI candidate |
| POST | `/api/method/ione_qms.api.ai.review_report_draft` | Accept/reject an AI report draft |
| POST | `/api/method/ione_qms.api.ai.review_quality_report_schedule` | Independently approve/reject a monthly report schedule |
| POST | `/api/method/ione_qms.api.ai.operate_quality_report_schedule` | Suspend, resume, or retire an approved report schedule |
| POST | `/api/method/ione_qms.api.ai.authorize_quality_report_recovery` | Let the exact configured report reviewer authorize one bounded replacement for the current terminal month |
| GET | `/api/method/ione_qms.api.ai.get_model_readiness` | Verify Qwen production readiness |
| GET | `/api/method/ione_qms.api.dashboard.get_command_center` | Read scoped command-center aggregates |
| POST | `/api/method/ione_qms.api.data_export.request_data_export` | Create a canonical, scope-frozen controlled export request |
| POST | `/api/method/ione_qms.api.data_export.approve_data_export` | Independently approve an authorized controlled export |
| POST | `/api/method/ione_qms.api.data_export.reject_data_export` | Independently reject an authorized controlled export |
| POST | `/api/method/ione_qms.api.source_document.approve_locator` | Independently approve an HTTPS source-document locator |
| POST | `/api/method/ione_qms.api.source_document.operate_locator` | Suspend, resume, or retire an approved locator |
| POST | `/api/method/ione_qms.api.source_document.locate_finding_evidence` | Resolve one authorized evidence deep link and write an immutable access log |
| POST | `/api/method/ione_qms.api.pdca.request_finding_recurrence_policy_approval` | Request independent approval of an explicit recurrence policy |
| POST | `/api/method/ione_qms.api.pdca.review_finding_recurrence_policy` | Approve or reject the frozen recurrence policy checksum |
| POST | `/api/method/ione_qms.api.pdca.operate_finding_recurrence_policy` | Suspend, resume, or retire an approved recurrence policy |
| POST | `/api/method/ione_qms.api.pdca.run_finding_recurrence_policy_scan` | Run one bounded, idempotent manual recurrence evaluation |
| POST | `/api/method/ione_qms.api.pdca.verify_pdca_measurements` | Independently verify governed PDCA effect measurements |
| POST | `/api/method/ione_qms.api.pdca.record_pdca_standardization_decision` | Record the immutable PDCA standardization conclusion and evidence hash |
| POST | `/api/method/ione_qms.api.surgery.review_staff_qualification_record` | Independently approve or reject a staff qualification |
| POST | `/api/method/ione_qms.api.surgery.review_surgery_authorization_record` | Independently approve or reject a scoped surgeon authorization |
| POST | `/api/method/ione_qms.api.surgery.review_surgery_procedure_policy_record` | Independently approve or reject a procedure-level governance policy |
| POST | `/api/method/ione_qms.api.surgery.retire_surgery_governance_definition` | Retire an approved qualification, authorization, or procedure policy |
| POST | `/api/method/ione_qms.api.surgery.record_surgery_mdt` | Atomically record immutable pre-operative MDT evidence and participants |
| POST | `/api/method/ione_qms.api.surgery.record_surgery_safety_checklist` | Record the exact approved pre-operative safety checklist |
| POST | `/api/method/ione_qms.api.surgery.request_surgery_emergency_exception` | Request an explicit bounded emergency exception |
| POST | `/api/method/ione_qms.api.surgery.review_surgery_emergency_exception` | Independently approve or reject an emergency exception |

## Quality meeting and action APIs

Every method in this group is POST-only. They re-check role, clinical scope,
named actor separation, current state, and frozen evidence on the server.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/method/ione_qms.api.quality_meetings.submit_quality_meeting` | Freeze and submit a complete Draft meeting |
| POST | `/api/method/ione_qms.api.quality_meetings.review_quality_meeting` | Independently approve or reject/cancel the submitted meeting |
| POST | `/api/method/ione_qms.api.quality_meetings.cancel_quality_meeting` | Cancel an eligible meeting with a reason |
| POST | `/api/method/ione_qms.api.quality_meetings.record_quality_meeting_held` | Record actual interval, complete attendance manifest, and held evidence |
| POST | `/api/method/ione_qms.api.quality_meetings.mark_meeting_minutes_pending` | Move a held meeting into minute preparation |
| POST | `/api/method/ione_qms.api.quality_meetings.submit_meeting_minute` | Submit a structured Draft minute |
| POST | `/api/method/ione_qms.api.quality_meetings.review_meeting_minute` | Independently approve/reject a minute and atomically derive decisions on approval |
| POST | `/api/method/ione_qms.api.quality_meetings.close_quality_meeting` | Close a meeting after its minute is approved |
| POST | `/api/method/ione_qms.api.quality_meetings.create_quality_action_item` | Create a scoped action from governed meeting/finding/PDCA/safety-event sources |
| POST | `/api/method/ione_qms.api.quality_meetings.start_quality_action_item` | Let the named owner start an open action |
| POST | `/api/method/ione_qms.api.quality_meetings.submit_quality_action_verification` | Submit completion evidence and effect result with an opaque `request_key`; exact retries are idempotent |
| POST | `/api/method/ione_qms.api.quality_meetings.review_quality_action_item` | Let the named verifier close or return an action and append an immutable checksum-chained verification receipt; requires `request_key` |
| POST | `/api/method/ione_qms.api.quality_meetings.cancel_quality_action_item` | Cancel an eligible action with a reason and `request_key`; pending verification creates an immutable cancel receipt |
| POST | `/api/method/ione_qms.api.quality_meetings.quality_action_workbench` | Read a bounded owner/verifier/oversight action queue |
| POST | `/api/method/ione_qms.api.quality_meetings.submit_quality_experience_share` | Submit structured de-identified experience for independent review |
| POST | `/api/method/ione_qms.api.quality_meetings.review_quality_experience_share` | Publish or reject a submitted version |
| POST | `/api/method/ione_qms.api.quality_meetings.retire_quality_experience_share` | Retire a published version while retaining its audit history |

## General constraints

- Use HTTPS outside the trusted internal network.
- Send `Content-Type: application/json`, except for the dedicated HL7 v2
  adapter, which requires `application/hl7-v2+er7` (or
  `application/hl7-v2`) and one explicit matching `charset`.
- Use source IDs and idempotency versions consistently.
- Do not retry a request with a new business identity.
- Pagination and record-count limits are enforced.
- Authorization is checked again at document and department scope.
- Responses never include secrets and should not be logged with clinical
  bodies at gateways.
- Controlled export creation returns the derived `scope_mode`, `hospital`,
  `campus`, and `department`. Those fields are server-derived and cannot be
  supplied or changed by the caller.
- Auditor decisions require a matching explicit User Permission and are
  limited to single-department or single-campus requests. Hospital-wide,
  multi-scope, and unscoped decisions require Medical Affairs.
- Repeated review/resume requests are idempotent. Only the holder of the
  current attempt/token claim can finalize; a superseded model result is rolled
  back and cannot commit business changes.
- Rule tests execute only in `Test` and require a named definition user. Their
  append-only receipts are bound to the current rule checksum and evaluator
  commit. Shadow executions are created only by live-event dispatch while the
  frozen window is open; named QC Reviewer/Medical Affairs users record
  one append-only `IONE QC Rule Shadow Feedback` through normal DocType create
  permission.
- Source-document location never reads or proxies source content. It returns
  one approved HTTPS URL only after linked-finding permission, clinical scope,
  endpoint/source allowlists, TLS, checksum, and exact evidence lineage pass.
  Unavailable configurations return a stable fail-closed code without a URL.
- Quality-meeting state transitions are POST-only. Approved agendas, minutes,
  derived decisions, published experience checksums, and terminal actions must
  not be changed through generic REST writes or direct database operations.
- Monthly report recovery requires `schedule`, the reviewer-visible
  `prior_task`, a stable `reason_code`, and `reason`. Replaying that same
  prior-task authorization returns its existing receipt/task; a stale
  unmatched task is rejected.

See `integration.md` for the signed event contract. OpenAPI generation and
publication remain a release deliverable. No OpenAPI artifact is accepted until
it is generated from the migrated, pinned release and validated against its
permission metadata.
