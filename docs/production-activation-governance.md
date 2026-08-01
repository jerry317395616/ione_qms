# Production activation governance

`IONE System Settings.production_mode` is a fail-closed runtime decision, not
an ordinary configuration flag. A successful build, migration, installation,
or model health check cannot enable it.

## Three accountable actors

1. A named `IONE QC Administrator` or `IONE Medical Affairs` assessor creates
   an `IONE Production Readiness Assessment`, attaches the private JSON
   evidence manifest, supplies every gate row, and submits it.
2. A different named `IONE QMS Auditor` independently approves or rejects the
   exact checksum-bound assessment.
3. A third named `IONE QC Administrator` or `IONE Medical Affairs` operator,
   different from both assessor and reviewer, activates the approved release.

`Administrator`, `Guest`, generic CRUD, direct REST writes, imports, and direct
`IONE System Settings` edits cannot approve or activate production. Activation,
deactivation, and revocation create append-only `IONE Production Activation
Event` records. Disabling production directly is allowed as a fail-closed
emergency action and atomically clears the dependent real-time and AI switches.

## Mandatory gates

An assessment has exactly one evidence row for each code below. No waiver or
sample default is accepted:

- `release_integrity`
- `hospital_scope`
- `source_contracts`
- `clinical_content`
- `data_reconciliation`
- `ai_governance`
- `identity_access`
- `security_privacy`
- `performance_capacity`
- `high_availability`
- `backup_restore`
- `clinical_uat`
- `change_approval`
- `oncall_observability`

The assessment is bound to the site, deployed immutable `IONE Release Record`,
current schema fingerprint, verified migration receipt, exact Git commit of
every installed app, evidence-manifest bytes, named signatories, evidence
timestamps, and a validity deadline of at most 90 days. Any drift, expiry,
revocation, manifest change, migration change, release change, or app-commit
change makes runtime production checks return disabled.

Configure a dedicated `ione_production_hmac_key_id` and at least 32-byte
`ione_production_hmac_key` in protected site configuration before submitting
the first assessment. Rotation uses `ione_production_hmac_verify_keys` for the
retained verification-only key ring. Evidence submission, independent review,
revocation, and activation events are domain-separated HMAC-signed; there is no
fallback to the Frappe encryption key or to another IONE key family.

## Evidence manifest

Upload a UTF-8 JSON file of at most 1 MiB as a private File attached to the
Draft assessment. It contains references and SHA-256 digests only; do not place
credentials, tokens, patient identifiers, clinical text, or evidence contents
in it.

```json
{
  "format": "ione-production-evidence-manifest-v1",
  "site": "manager.example.internal",
  "release_record": "<deployed IONE Release Record>",
  "schema_revision": "<current assessment schema_revision>",
  "migration_completion_receipt": "<current assessment receipt>",
  "generated_at": "2026-08-01 12:00:00",
  "artifacts": [
    {
      "id": "SECURITY-ACCEPTANCE-2026-08-01",
      "sha256": "<64 lowercase hexadecimal characters>",
      "gate_codes": ["identity_access", "security_privacy"],
      "owner": "<named hospital security signatory>",
      "signed_at": "2026-08-01 11:00:00",
      "expires_at": "2026-08-31 23:59:59"
    }
  ]
}
```

Each child row's reference, digest, signatory, signature time, expiry, and gate
must exactly match one manifest artifact. The manifest File becomes immutable
when the assessment leaves Draft.

Use the offline hash-only generator described in
`production-evidence-manifest-tool.md` to build the manifest and the matching
14 child rows from signed evidence files. Its example register is deliberately
non-production data and must be replaced with hospital-approved identities,
timestamps, bindings, and artifact paths.

## Release record manifest

The linked append-only `IONE Release Record` must have `Deployed` status and a
named deployer, deploy candidate, image, database and files backup identifiers,
test report, and this `manifest_json` contract:

```json
{
  "format": "ione-release-manifest-v1",
  "apps": {
    "frappe": "<40-character Git SHA>",
    "flow": "<40-character Git SHA>",
    "ione_qms": "<40-character Git SHA>"
  },
  "sbom_sha256": "<64 lowercase hexadecimal characters>",
  "ci_evidence_sha256": "<64 lowercase hexadecimal characters>",
  "deployment_evidence_sha256": "<64 lowercase hexadecimal characters>"
}
```

`apps` must list every installed app and must exactly match the running Bench,
not only the three entries shown in the abbreviated example.

## Governed API

Every transition is POST-only:

- `ione_qms.api.production.submit_assessment`
- `ione_qms.api.production.approve_assessment`
- `ione_qms.api.production.reject_assessment`
- `ione_qms.api.production.activate_production_mode`
- `ione_qms.api.production.deactivate_production_mode`
- `ione_qms.api.production.revoke_assessment`

Activation optionally enables real-time rules and governed AI. AI activation
also performs the live authenticated Qwen readiness check. The runtime still
applies every rule-release, evaluation, policy, scope, and human-confirmation
gate; the production assessment cannot bypass those domain controls.
