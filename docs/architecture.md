# IONE QMS architecture

## Scope

IONE QMS is one custom Frappe application split into nine modules. Frappe,
ERPNext, Flow, Press, and other upstream applications remain unmodified. The
application owns all hospital-quality data models, deterministic decision
logic, integrations, dashboards, and AI governance hooks.

| Layer | Responsibility |
| --- | --- |
| Foundation | Hospital, campus, department, ward, staff, patient index, encounter index |
| Quality Standards | Standards, versioned clauses, deterministic rules, rule tests |
| Clinical Quality | Events, executions, findings, evidence, record/surgery/safety quality |
| Indicators | Versioned definitions, calculations, results, details, alerts |
| Improvement | Rectification, verification, root cause, measures, PDCA |
| Integration | Read-only sources, allowlisted connectors, mappings, messages, reconciliation |
| Flow AI | Policies, releases, tests, evaluations, tasks, candidates, drafts, run links |
| Analytics | Daily/monthly/finding/surgery/agent facts and command center |
| Administration | Settings, security policies, feature flags, release records |

## Runtime chains

### Deterministic real-time quality control

```text
Signed/idempotent event
  -> Integration Message
  -> normalized Clinical Quality Event
  -> short queue
  -> published Rule Versions
  -> QC Executions
  -> deduplicated candidate QC Findings
  -> human confirmation
```

A rule condition matching means the reviewed undesirable condition exists and
therefore produces `Failed`. Missing data produces `Insufficient Data`, never a
finding. Approved exclusions produce `Excluded`. Every execution retains rule
version, normalized context hash, bounded evidence, result, timestamps, and
error category.

### Rectification and improvement

```text
Confirmed finding
  -> rectification plan and actions
  -> department approval
  -> functional review
  -> objective verification
  -> closure or rework
  -> indicator/PDCA feedback
```

No technical administrator, generic Flow tool, or AI task can replace the
business approvals.

### Governed AI

```text
Authorized user
  -> IONE AI Analysis Task
  -> active Agent Policy
  -> committed phase/attempt/token/lease claim
  -> dedicated service user
  -> explicit IONE imported tools
  -> Flow Agent + internal Qwen
  -> draft/candidate only
  -> mandatory review where configured
```

Prompt content, medical records, external documents, and tool output are
untrusted data. The deterministic rule engine remains the source of objective
quality findings. AI can summarize, analyze trends, and propose candidates; it
cannot publish standards, approve or close findings, impose penalties, or
write source systems.

Every initial or resumed execution commits a task claim containing phase,
attempt, an opaque token, lease, heartbeat, and an append-only Claim Event
before crossing the Flow/model boundary. Finalization holds the task advisory
lock and row lock and succeeds only through token/attempt compare-and-swap. A
worker that has lost the claim rolls back its model result and cannot link or
commit it.

A paused Flow Run can produce several immutable `IONE Flow Run Link`
revisions. Each revision has a unique `revision_key`, `run_iteration`,
execution-token hash, and complete payload/transcript/tool hashes. Governed IONE
runs prohibit session attachments and retain a canonical empty-attachment hash.
Consumers select the latest revision by iteration and creation order. Stale
recovery first reconciles an exact terminal orphan, renews a lease only while
the original RQ job remains active, and fails an expired Resume claim closed
instead of re-executing an approved write tool.

## Data ownership

- Source HIS/EMR/LIS/PACS/HRP systems are authoritative and read-only to QMS.
- Integration messages preserve a canonical payload hash and source identity.
- QMS owns standards, executions, findings, evidence, indicators, workflows,
  audit records, and derived analytics.
- Large source payloads can be moved to an approved encrypted object store;
  database rows retain references and integrity hashes.
- Analytics facts are derived and rebuildable. Evidence and audit records are
  immutable and are never removed by normal retention jobs.

## Scalability boundaries

- Realtime event evaluation uses the `short` queue.
- Bulk scans and indicators use the `long` queue.
- AI uses a dedicated `ai` queue when configured and otherwise the `long`
  queue. Initial execution and approval resume use the same queue selector.
- Scheduled jobs use bounded batches, locks, idempotency keys, and resumable
  cursors.
- Dashboard endpoints aggregate in the database and return no patient
  identifiers.
- The deployment target is at least 1,000 concurrent users, 5 million events
  per day, and 10 million rule evaluations per day. These are release gates,
  not assumptions; each pinned build requires a representative load report.

## Availability targets

| Target | Requirement |
| --- | --- |
| Application availability | at least 99.9% |
| Realtime rule p95 | at most 3 seconds |
| Interactive page p95 | at most 2 seconds |
| AI analysis target | at most 10 seconds where model capacity permits |
| RPO | at most 15 minutes |
| RTO | at most 2 hours |

The application degrades safely: model unavailability pauses or fails AI tasks
without writing formal findings; integration outages retry without duplication;
analytics lag does not block deterministic realtime quality control.
