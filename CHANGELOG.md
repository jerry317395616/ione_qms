# Changelog

All notable changes to IONE QMS are documented here.

## Unreleased

- Add a distinct governed `DIP` grouping dimension alongside `DRG`, including
  read-only integration mapping, encounter/event propagation, rule evidence,
  clinical-quality projections, indicator lineage/results, daily/monthly facts,
  analytics filtering, metadata validation, and regression coverage.
- Initial production implementation for Frappe 17 develop and Frappe Flow develop.
- Add a fail-closed production activation gate with 14 mandatory evidence
  domains, private immutable manifests, exact release/schema/commit binding,
  three-person separation of duties, domain-separated HMAC signatures, and
  append-only activation/deactivation/revocation events.
- Add a dependency-free, atomic hash-only evidence-manifest generator that
  rejects missing/overlapping gates, technical signatories, invalid validity
  windows, empty artifacts, and accidental output overwrites.
