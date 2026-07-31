# Standard version comparison

`ione_qms.api.rules.get_standard_version_difference` is the read-only contract for
reviewing two versions of one quality standard and producing the rules that require
business review.

## Access

The endpoint accepts only a named user with one of these business roles and read
permission on all four definition types used by the comparison:

- IONE QC Administrator
- IONE QC Reviewer
- IONE Medical Affairs
- IONE QMS Auditor

Guest, Administrator, and any user carrying `IONE Agent Service` are rejected even
if another role is also assigned. The service reads only Standard Version, Standard
Clause, Rule, and Rule Version definition tables. It does not query patients,
encounters, clinical events, findings, integration messages, or payloads.

## Deterministic lineage

Both version records must exist and must link to the same `IONE QC Standard`.
Standard-level field differences cover effective dates and the governed scope JSON.
The response carries each version's lifecycle state, stored checksum, approval
metadata, clause count, and an observed snapshot checksum.

`clause_code` is the sole cross-version clause identity. Heading, content, and
keywords are compared by exact SHA-256 digest plus null state and character count.
No text-similarity or semantic matching is performed. A changed clause code is
therefore reported as a removal and an addition.

The affected-rule list is evidence based:

- a Rule Version relation must have an exact `standard_snapshot` match; and
- a clause-level relation must have an exact `standard_clause_snapshot` match to a
  changed baseline or target clause;
- a Rule parent relation must have exact `standard` and, for clause-level impact,
  exact `standard_clause` links.

Non-retired rule relations are returned with their source record, status, checksum,
clause link, and impact basis. Added clauses without an explicit rule relationship
do not cause the service to invent a rule.

## Bounds and pagination

One comparison is capped at 5,000 clauses per version, 5,000 affected rule
relations, and 2,500 distinct affected rules. Each of the clause-change,
affected-rule, and rule-relation result sets has an independent offset and page
length; page length is capped at 25 and offset at 10,000.

Policy text is never loaded into the response without a bound. Each returned text
preview is at most 400 characters and includes the exact source character count,
SHA-256 digest, null flag, and truncation flag. Oversized source definition fields
fail closed instead of being processed without review.

Every response includes `comparison_checksum`. A caller should send that value as
`expected_comparison_checksum` on every later page. If either version, its clauses,
or an explicit rule relationship changes between requests, the endpoint rejects
the page and the caller must restart from the first page.

Example:

```text
GET /api/method/ione_qms.api.rules.get_standard_version_difference
    ?baseline_version=STD-2026-1
    &target_version=STD-2026-2
    &change_start=0
    &change_page_length=25
    &rule_start=0
    &rule_page_length=25
    &relation_start=0
    &relation_page_length=25
```
