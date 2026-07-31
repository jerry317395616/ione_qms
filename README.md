# IONE QMS

IONE QMS is the single custom Frappe application for the hospital-wide medical
quality management platform described in the IONE V1.0 technical blueprint.
It covers standards, deterministic rules, clinical findings and evidence,
indicators, rectification and PDCA, hospital-system integration, analytics,
and governed Frappe Flow agents.

Repository status: 104-DocType/9-module static implementation baseline.
Realtime clinical rules and AI remain disabled. Production readiness remains
NO-GO until the dated release, clinical, integration, security, capacity,
backup/restore, and Press acceptance gates pass.

## Architecture contract

- Frappe Framework and Frappe Flow remain unmodified upstream applications.
- All IONE business code lives in this repository.
- The default development branch is `develop`; production deploys pin tested
  commit SHAs for Frappe, Flow, and IONE QMS.
- Deterministic rules decide objective quality conditions. AI produces only
  drafts or candidate findings that require policy checks and, where relevant,
  human review.
- Source clinical systems are read-only from this application. Writes are
  limited to approved Frappe business objects.

## Modules

1. IONE Foundation
2. IONE Quality Standards
3. IONE Clinical Quality
4. IONE Indicators
5. IONE Improvement
6. IONE Integration
7. IONE Flow AI
8. IONE Analytics
9. IONE Administration

## Local development bench only

Install Flow before IONE QMS:

```bash
bench get-app --branch develop flow https://github.com/jerry317395616/flow
bench get-app --branch develop ione_qms https://github.com/jerry317395616/ione_qms
bench --site <site> install-app flow
bench --site <site> install-app ione_qms
bench --site <site> migrate
```

These commands are not the production deployment procedure. `manager` must be
built, deployed, and installed through immutable Press App Release, Deploy
Candidate, and Site App workflows. Direct edits inside a running container are
not supported.

## Configuration

After installation:

1. Configure hospital, campus, departments, and data-retention policies.
2. Configure each source system and endpoint with least-privilege credentials.
3. Configure the Qwen endpoint and encrypted credential in Flow, then
   allowlist the approved model host and configure governance/retention in IONE
   AI Settings.
4. Import or approve standards, indicators, and rule versions.
5. Run rule replay and shadow mode before publishing clinical rules.

See `docs/` for architecture, operations, security, interfaces, traceability,
and release runbooks.

Start with:

- `docs/architecture.md`
- `docs/security.md`
- `docs/privacy-identity-access.md`
- `docs/clinical-governance.md`
- `docs/integration.md`
- `docs/ai-qwen.md`
- `docs/testing-and-acceptance.md`
- `docs/hospital-production-inputs.md`
- `docs/release-and-rollback.md`
- `docs/production-readiness-checklist.md`
