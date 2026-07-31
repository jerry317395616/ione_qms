# Clinical configuration governance

The application implements the platform and the reviewed deterministic
execution mechanism. It does not invent hospital clinical policy.

## Publication workflow

1. Import a source standard and retain provenance.
2. Create a version and structured clauses.
3. Draft rules or indicators that cite exact clauses.
4. Add Positive, Negative, Exclusion, Boundary, Missing Data, Real World, and
   Regression test cases.
5. Obtain named clinical-expert review and quality-department approval.
6. Replay against an approved historical sample.
7. Run shadow mode and measure false positives/negatives.
8. Publish an immutable version with effective dates and checksum.
9. Monitor drift, data quality, override, appeal, and alert rates.
10. Retire through a new version; never edit historical published evidence.

Standard-version checksums bind the parent identity/provenance snapshot, a mandatory
private source-File SHA-256 over real bytes, and canonical ordered hashes of every clause.
A URL may identify provenance but cannot by itself become `Verified` or `Approved`.
`archive_standard_source_url` downloads only an administrator-allowlisted, public-global
HTTPS host into an attached private File with bounded size and redirects. The downloader
connects to the exact public IP set that passed DNS validation while retaining hostname
TLS/SNI verification, so DNS rebinding cannot redirect the socket to an internal service.
PDF, macro/external-link-free DOCX, and UTF-8 plain text are the only accepted formats;
declared MIME type, file signature, and extension must agree. Legacy URL-only
versions are quarantined by the bounded lineage migration. Rule
and indicator versions cite one exact Approved Standard Version and one exact
Published Clause, copy their checksum/content hash and authority JSON, and
must fit inside that authority's effective interval. Clause
publication/retirement is derived from the parent version; clause text and a
reviewed source File cannot be inserted, replaced, moved, optimized, deleted,
or edited after review begins. A Standard Version cannot retire while an
active exact rule/indicator dependency remains. Changing identity, provenance,
or clauses therefore requires a new Standard Version and fresh approval.

The seeded 2026 national-quality blueprint is a draft template only. It cannot
produce a production finding until the hospital supplies source mappings,
approved definitions, exclusions, thresholds, attribution, blocking points,
and test samples.

## Rule acceptance

Each rule version requires:

- source standard and clause;
- business owner and clinical approver;
- event trigger and evaluated population;
- deterministic condition and exclusions;
- missing-data behavior;
- severity and action;
- effective period;
- replay sample and expected results;
- shadow-mode report;
- checksum and release record.

Before expert review begins, the server snapshots the parent rule code, name,
type, exact Standard Version/Clause authority, category, risk, owner, and
description into the rule version. A Python plug-in key is pinned in that same
version. Selection, execution, findings, evidence, and notifications use only
those snapshots. The parent becomes an immutable identity record once a
reviewed version exists; changing any governed semantics requires a new rule
version.

Test-definition edits reset every prior result. Leaving `Test` requires all
seven categories and immutable passing receipts for the exact current rule
checksum and deployed evaluator commit. The test definitions then freeze.
Production thresholds are defined by approved samples and the frozen online
shadow window. Passed/Excluded review targets are selected by deterministic
site-keyed HMAC strata; reviewers supply independent gold labels, and both
false-positive and false-negative rates must stay below their frozen maxima.
Missing-data defects, rule errors, or unresolved reviews fail closed; every
finding must reproduce from stored version and evidence.

## Indicator acceptance

Each indicator version requires numerator, denominator, inclusion, exclusion,
unit, aggregation, dimensions, period, target, direction, source lineage,
calculator key, and zero-denominator behavior. Results retain the exact
version, calculation run, numerator, denominator, value, target, dimensions,
details, and source watermark.

The governed registry has exactly nine canonical dimensions: `hospital`,
`campus`, `department`, `ward`, `medical_group`, `physician`, `disease`,
`surgery`, and `drg`. `medical_staff` is accepted only as a compatibility
alias and is canonicalized to `physician` before any query, series, receipt, or
checksum is built. Every registered dimension declares its source-field
adapters and enumeration/authorization scope. Publication and calculation run
the same dataset/calculator capability check; an unsupported dimension or
dimension combination fails closed.

Before review, the server materializes the indicator's code, name, category,
definition, formula, calculator, targets, direction, and exact
Standard-Version/Clause authority into the version checksum. Runtime
calculation and display do not fall back to the mutable parent. The parent
identity/formula/target fields freeze when any version leaves Draft.

Publication additionally freezes one Active source-mapping record and its
covered effective interval, source system, mapping version/checksum, normalized
query contract, calculator semantic version, and executable code hash. Every
calculation creates a canonical input receipt containing those anchors, the
exact period and canonical dimensions, and deterministic count/hash manifests
of the source rows. The receipt hash is the idempotency key.

Recalculation never overwrites a result or detail. A changed source manifest,
query, mapping, or code input creates the next append-only revision for the
same indicator-version/period/dimension series. One advisory lock plus a
database row lock atomically advances the series' `IONE Indicator Result
Pointer`; daily/monthly projections and ordinary APIs use only that pointer.
Auditors can verify stored checksums or perform a read-only reproduction. Old
rows that cannot prove an exact receipt are marked `Legacy Quarantined` and
block migration completion; the repair process never invents a watermark.
Result-detail consumers apply one mandatory rule: the parent result must be the
current pointer revision and the exact detail must have no approved quarantine
disposition. The rule is enforced in list/report permission SQL, direct document
permission, controlled export, and indicator source calculation. Consequently,
superseded details and details governed by `Exclude Legacy` cannot be read,
exported, or counted even though their append-only audit record is retained.

Effective periods are closed intervals. Daily and rolling versions may start
or end on any date. Weekly versions start on Monday and end on Sunday; monthly,
quarterly, and annual versions use their corresponding calendar boundaries.
Rolling calculation waits until the entire reviewed window fits inside the
effective interval. The scheduler and direct calculation service reject
partial periods. Retirement sets `effective_to` once, retains all historical
results, and cannot overlap another Published or Retired version of the same
indicator.

## AI acceptance

AI evaluation sets are separate from deterministic rule samples. All ten
governed quality/security categories are mandatory, with confusion matrices
and independently approved, signed, versioned thresholds. Security acceptance
allows no cross-scope overreach, forbidden tool call, PII canary leakage, or
missing/scope-inconsistent evidence. Cases execute real model-requested tools
only against rollback-only namespaced synthetic fixtures; they never create a
formal finding or action. Evaluation always uses Flow `auto_approve=False`.
An Agent with confirmation tools cannot be authorized by rollback-only evidence:
the observed pause fails closed because the committed IONE approval/reviewer/resume
protocol requires a dedicated synthetic site and a separately governed receipt design.
High-risk outputs receive 100% human review.
Model-generated content remains a draft even when evaluation scores are high.

## Change control

Every production change links:

- requirement and clinical source;
- configuration/version checksum;
- code release commit;
- migration;
- test and replay evidence;
- approvers;
- deployment candidate;
- backup and rollback point;
- post-deployment validation.

Emergency deactivation uses feature flags or version status. It does not edit
official application code, delete evidence, or rewrite history.

Legacy versions with missing exact lineage are never inferred after review.
The bounded post-migrate repair reconstructs only Draft versions from current
governed parents; ambiguous reviewed versions are marked `Quarantined` and
receive an append-only `IONE Definition Lineage Receipt`. A quarantine in any
review/active state blocks migration completion. It may only be retired by a
named QC Reviewer or Medical Affairs user with an explicit end date; production
requires a new lineage-verified replacement.

Reviewed or quarantined Standard, Rule, and Indicator versions are retained
records and cannot be deleted, including by technical Administrator paths.
Parents with any retained version cannot be deleted.

## Finding recurrence and structured PDCA

Recurrence escalation is disabled until a named hospital reviewer supplies a
hospital scope, exact rule-and-scope grouping, threshold, window, and effective
period, and a different named reviewer approves the checksum. Approved
definitions are immutable, cannot overlap, and can only be suspended, resumed,
or retired through governed operations.

Daily or manual scans use only human-confirmed findings whose appeals have not
overturned the finding. The bounded snapshot contains governed rule/scope,
internal finding links, and hashes; it excludes patient identity and narrative.
A threshold match creates only a `Proposed` special PDCA project. Human users
must still define and approve the clinical problem, goal, root-cause analysis,
and action plan.

PDCA supports hierarchical Five Why, fishbone categories, deterministic Pareto
rank/count/cumulative percent, accountable measures, and indicator-result
baseline/target/actual measurements. Measuring requires an independent
verification receipt. Closure additionally requires a human standardization
decision, evidence reference, and SHA-256. Verified measurements,
standardization receipts, terminal projects, recurrence evaluations, and run
snapshots are immutable.

When one verification request references multiple indicator series, all current
result pointers are resolved and locked in global `series_key` order before any
measurement hash is written. Evidence hashing still follows the clinical
baseline/actual and requested measurement order; lock ordering is only a
concurrency control and cannot change evidence semantics.

## Quality meetings and experience sharing

Meeting governance is a separate human-control plane. A complete scoped meeting
is submitted to its explicitly configured approver; approval freezes the agenda,
material references, and attendee plan. Recording that it was held requires the
actual interval, chair attendance, a result for every planned attendee, and
evidence. Closure requires a separately authored and independently approved
minute.

Minute approval is concurrency-safe and atomically derives immutable decision
records. Follow-up actions require an accountable owner, due date, completion
evidence, effect result, and a different named verifier before closure.
Published experience is versioned, effective-dated, checksummed, and sourced
only from a confirmed/closed safety event or approved minute. It requires
structured de-identification, attestation, and independent publication review;
source narratives and AI report text are never auto-published.

The hospital must approve meeting types, committee/quorum/notice rules, named
actor mappings, escalation SLAs, evidence retention, and its de-identification
policy before activation. See `quality-meetings-and-actions.md` for the state
and evidence contract.
