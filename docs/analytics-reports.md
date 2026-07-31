# Governed analytics reports

IONE QMS supplies nine standard Frappe Script Reports in the **IONE Analytics**
workspace:

- **IONE Rule Quality**
- **IONE Indicator Trend and Quality Profile**
- **IONE Finding Distribution and Closure**
- **IONE Integration Reconciliation**
- **IONE AI Usage and Adoption**
- **IONE Medical Record Review Governance**
- **IONE Surgery Governance and Outcomes**
- **IONE 2026 National Ten-Goal Progress**
- **IONE Improvement Governance Overview**

All reports are synchronous, read-only, and bounded. They use `frappe.get_list`
after both a named-role check and `frappe.has_permission` check. Clinical DocType
permission-query hooks therefore remain active for every base and related read.
No report uses raw SQL, `frappe.get_all`, `ignore_permissions`, or patient/encounter
payload fields.

Guest, Administrator, and every user carrying `IONE Agent Service` are rejected,
including service users that also carry a business role. Each report has a closed
business-role allowlist matching the read permissions of all DocTypes it needs.

## Time and volume bounds

The default window is the latest 30 inclusive days. `from_date` must not follow
`to_date`, and a window may not exceed 366 inclusive days.

The hard bounds are:

- 500 returned report rows;
- 10,000 permission-filtered source rows;
- 5,000 permission-filtered aggregate groups.
- 1,000,000 permission-filtered base records for a grouped aggregation.

The service fetches one extra row and explicitly rejects an over-limit request.
Users must narrow date, organization, indicator, source, endpoint, task type, or
policy filters. Results are never silently truncated.

Organization filters are hospital, campus, department, and ward. They are applied
to the same permission-filtered clinical rows used for the report. Existing
clinical-scope query hooks exclude inconsistent hierarchy and tenant records.

## Metric contracts

### Rule quality

Execution counts use the governed `IONE QC Execution.result` states:
Passed, Failed, Excluded, Insufficient Data, and Error.

- **Failed Hit Rate** = Failed / all executions.
- **Excluded Exemption Rate** = Excluded / all executions.
- **Human Appeal Overturn Rate** =
  Approved / (Approved + Rejected) appeals.

The last metric is intentionally not labelled a generic false-positive rate.
Submitted and withdrawn appeals have no completed human decision and are excluded
from that denominator. The report exposes submitted feedback, reviewed, approved,
and rejected counts separately. An appeal whose linked finding is not visible
through the caller's Finding permission query is counted only in the non-sensitive
unattributed notice and is never guessed into a rule.

### Indicator trend and quality profile

Every row and chart point is the pointer-selected current revision of one
governed `IONE Indicator Result`. Values are not averaged, summed, or
reinterpreted. Frozen targets and scope are shown beside links to the Indicator
Result, Indicator Version, calculation record, version lifecycle state, and
checksum. The stored revision, input-receipt hash, and result checksum preserve
the exact report input while prior revisions remain append-only audit records
and are excluded from ordinary report/API reads. `lineage_json`, source-row
identifiers, patient, encounter, and staff identifiers are not returned.

### Finding distribution and closure

The selected cohort is findings detected in the requested date window.
Rectification and verification columns show the current state of permission-visible
records linked to that cohort. `Closed` and `Appeal Approved` are separate:
an overturned finding is not misreported as an operationally closed defect.
Overdue means an active finding or rectification with a due date earlier than the
report generation date.

### Integration reconciliation

The report shows each reconciliation window and its recorded source, message,
event, execution, error, and signed difference counts. It does not sum volumes
across rows because reconciliation windows can overlap. Status summaries count
reconciliation records, not source transactions.

### AI usage and adoption

Agent Analysis Fact has one governed fact per Analysis Task.

- **Successful Tasks With Accepted Candidate** counts successful task facts with
  at least one accepted AI candidate.
- **Successful Task Acceptance Rate** = those tasks / successful tasks.
- **Accepted Candidates** remains a separate count because one task can contribute
  several accepted candidates and that count is not a percentage.

The report returns organization scope, governed policy/agent/model references,
task outcomes, duration, and token totals. It does not return prompts, outputs,
patient/encounter identifiers, requester identity, or review text.

### Medical record review governance

Each row is one immutable terminal-record sampling batch. The report shows the
frozen sampling-policy checksum and declared population/sample sizes beside
permission-visible assignment, coder decision, expert decision, latest archive
decision, and source acknowledgement counts. Human outcomes remain explicit
(`Pass`, `Defect`, `Needs Correction`, or `Unable to Determine`) and are never
renamed as false positives. Review, archive, request, and acknowledgement
checksum coverage is reported without selecting patient, encounter, staff,
source-record identity, comments, evidence, or any clinical narrative.

Declared batch counts can exceed visible assignment counts for a scoped coder or
expert because every linked read remains permission-filtered. This is stated in
the report message and is not presented as missing clinical data.

### Surgery governance and outcomes

Rows are aggregated by exact organization scope, surgery level, and governed
procedure-policy link. They show the policy version/checksum and exact counts for
authorization, level-IV MDT, safety checklist, emergency exception,
pre-anesthesia, intraoperative monitoring, recovery, postoperative follow-up,
cancellation, unplanned surgery, unplanned return, complication, and mortality
states. No case-level link, patient/encounter/staff identifier, source identifier,
procedure narrative, exception reason, MDT conclusion text, or clinical detail is
selected.

The report does not infer whether an absent stage was required. It reports the
stored state and separately shows the approved policy's `require_mdt` flag.

### 2026 national ten-goal progress

The standard filter is mandatory. The selected record must be a Published
`National Policy`; the report never guesses which local Standard represents the
2026 ten national goals. Active indicators must link through a Published clause
to an Approved Standard Version with a checksum. Only Published Indicator
Versions effective in the requested period are displayed.

Every value row is one exact permission-filtered, pointer-selected Indicator
Result revision. Rows retain the Standard Version and Indicator Version
checksums, effective dates, period, scope, calculator, stored
numerator/denominator/value, approved definition targets, and frozen result
target snapshot. Values are never averaged across versions. Two versions for
the same indicator, period, and scope are rejected as ambiguous. An approved
definition with no visible result produces an explicit `No Data` row; missing
targets are labelled `Target Not Configured`, never filled with an application
default.

### Improvement governance overview

Rows aggregate governance receipts by exact organization scope across recurrence
evaluations, special-recurrence PDCA projects, quality meetings, approved
minutes, derived decisions, action items, and published experience versions.
Policy/evaluation, recurrence snapshot, agenda, minute, decision,
standardization, and publication checksum coverage is visible.

`Projects Past Effect Verification Gate` counts only projects in `Measuring` or
`Closed`; the governed PDCA workflow requires verified immutable effect
measurements before those states. `Independently Verified Actions` counts Closed
action items with verifier and closure timestamps. The report never reads finding
content, root-cause narrative, meeting discussion, completion evidence, effect
text, or experience content.
