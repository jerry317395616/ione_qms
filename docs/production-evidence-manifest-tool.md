# Production evidence manifest tool

The production gate accepts hashes and accountable signatories, not raw
clinical, security, or infrastructure evidence. Use the dependency-free
generator to hash the signed source artifacts and produce both the private
manifest and the 14 matching child-table rows.

The tool does not sign evidence, decide whether evidence is sufficient, or
replace an independent review. It never copies source paths or artifact bytes
into its outputs. Run it on a controlled workstation whose clock, filesystem,
and evidence custody have been approved by the hospital.

## Register

Copy `docs/production-evidence-register.example.json` outside the repository
and replace every example value. Each artifact must be a non-empty regular
file. Relative paths resolve from the register file. Every mandatory gate must
appear exactly once; an artifact may cover multiple gates, but overlapping or
missing gate assignments are rejected.

All timestamps use ISO 8601. `signed_at` cannot be later than `generated_at`,
and every artifact must remain valid through `assessment_valid_until`.
`Administrator`, `Guest`, empty files, unknown gates, duplicate gates,
duplicate artifact IDs, and malformed registers fail closed.

## Generate

From the application repository or an installed Bench environment:

```bash
python -m ione_qms.release_tools.production_evidence \
  /controlled/release/evidence-register.json \
  --manifest-output /controlled/release/production-evidence-manifest.json \
  --gate-rows-output /controlled/release/production-gate-rows.json
```

The command preflights both distinct output paths and refuses to overwrite
either existing output unless `--force` is explicit. It writes each JSON file
atomically and prints the exact manifest SHA-256 plus the row count.

1. Review the generated manifest and printed digest under the hospital change
   record.
2. Attach the manifest as a private File to the Draft
   `IONE Production Readiness Assessment`.
3. Enter the generated gate rows in their required order. Do not import them
   into any other assessment.
4. Have the accountable assessor submit the assessment, the independent
   `IONE QMS Auditor` approve it, and a third named operator activate it.

The server independently re-reads the private File, recomputes its digest,
matches every row, revalidates the deployed release/schema/migration/app SHA
binding, and rejects stale, expired, altered, or actor-reused evidence.
