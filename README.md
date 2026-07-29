# IONE QMS

IONE QMS is the single custom Frappe application for the hospital-wide medical
quality management platform described in the IONE V1.0 technical blueprint.
It covers standards, deterministic rules, clinical findings and evidence,
indicators, rectification and PDCA, hospital-system integration, analytics,
and governed Frappe Flow agents.

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

## Installation

Install Flow before IONE QMS:

```bash
bench get-app --branch develop flow https://github.com/jerry317395616/flow
bench get-app --branch develop ione_qms https://github.com/jerry317395616/ione_qms
bench --site <site> install-app flow
bench --site <site> install-app ione_qms
bench --site <site> migrate
```

Production installation is performed through Press using immutable App
Releases and a Deploy Candidate. Direct edits inside a running container are
not supported.

## Configuration

After installation:

1. Configure hospital, campus, departments, and data-retention policies.
2. Configure each source system and endpoint with least-privilege credentials.
3. Configure the local Qwen OpenAI-compatible endpoint in Frappe Flow and IONE
   AI Settings.
4. Import or approve standards, indicators, and rule versions.
5. Run rule replay and shadow mode before publishing clinical rules.

See `docs/` for architecture, operations, security, interfaces, traceability,
and release runbooks.
