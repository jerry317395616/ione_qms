# Patient identity and PHI access governance

IONE QMS keeps direct patient identity out of the normal Frappe read surfaces.
The control applies to Desk forms, REST document reads, list and report views,
link search, standard print, and both native and governed exports.

## Safe-by-default behavior

- `source_patient_id`, `patient_name`, `date_of_birth`,
  `identification_hash`, `phone_hash`, `source_encounter_id`, and
  `encounter_no` are masked permlevel-9 fields. The same release-controlled
  registry protects raw integration payloads/source IDs, evidence payloads,
  AI task input, and AI access-log snapshots.
- No role receives permlevel-9 DocPerm. The normal title and search
  fields use only `patient_key` or `encounter_key`.
- A request-boundary guard rejects protected identity field names in generic
  Frappe resource, list, filter, report, count, group, and search requests.
  This closes filter-only inference even though Frappe field-level permission
  primarily controls selected return columns.
- Runtime `Custom DocPerm`, `Custom Field`, and `Property Setter` overrides
  cannot target any registry-protected DocType. Saved reports, prepared
  reports, charts, number cards, list filters, notifications, auto-email
  reports, print formats, webhooks, workflows, scripts, and forms cannot target
  or reference protected fields.
  Install and migrate fail if a legacy bypass artifact is discovered.
- The controlled export service rejects the direct-identifier field registry
  even if metadata is later misconfigured.
- `IONE PHI Identity Reader` is created but is never assigned automatically.
- No disclosure policy is seeded. Therefore a fresh installation denies every
  identity disclosure until the hospital approves and enters its own
  minimum-necessary matrix.
- `Administrator` is not eligible for governed disclosure. The request-boundary
  guard also denies technical-Administrator generic reads, reports, print,
  export, download, and identity-bearing side-car objects such as File,
  Comment, Communication, Version, ToDo, and Prepared Report. Named,
  least-privilege users are mandatory.

## Policy lifecycle

Each `IONE PHI Disclosure Policy` contains exactly one purpose code and one
hospital scope. Campus, department, and ward may narrow that scope. Its
`allowed_fields_json` maps supported record types to the minimum fields that
may be returned. `allowed_reader_roles_json` contains the business roles that
may use that purpose. Per-user and per-record disclosure limits are part of
the checksum-bound policy and are enforced in one-minute Redis windows.

Example policy fragments:

```json
{
  "IONE Patient Index": ["patient_name", "source_patient_id"],
  "IONE Encounter Index": ["encounter_no"]
}
```

```json
["IONE Physician", "IONE Department QC Officer"]
```

The author must provide an approved hospital governance identifier in
`external_approval_reference`. A named author submits a Draft through the
governed POST API. A different named Medical Affairs or Auditor user verifies
the checksum and approves it. A future-dated version enters `Scheduled`; an
immediately effective version atomically supersedes the exact-scope Active
version. Request-time activation plus the minute scheduler swaps a due
Scheduled policy under the family lock without an access gap. Submitted
semantics are immutable; amendments require a new policy version. Active or
Scheduled policies can only be retired through the governed POST API. Policy
resolution executes an exact maximum-eight scope query and selects the unique
most-specific effective scope.

## Disclosure path

`POST /api/method/ione_qms.api.privacy.read_phi_identity` requires:

1. a named, authenticated user;
2. both `IONE PHI Identity Reader` and a policy-allowed business role;
3. clinical authority independently attributable to that same allowed role
   for the exact patient or encounter;
4. an active, effective, checksum-valid policy for the exact purpose and scope;
5. a non-empty minimum field set contained in that policy; and
6. a new opaque request ID for every disclosure.

The service uses a dedicated, versioned `ione_phi_hmac` key; it never falls
back to the site's general encryption key. The active key ID is recorded and
retained verification-only keys keep historical policies and receipts
verifiable across rotation. Every disclosure first takes one application-wide
PHI-authorization advisory lock. This deliberately serializes the already
rate-limited disclosure decision so two users cannot deadlock while locking
overlapping mutable grants in opposite staff/patient order. Within that lock it
locks the User, role assignments, User Permissions, current Medical Staff
assignments, Patient/Encounter lineage, Source System, Hospital, Campus,
Department, and Ward dependencies in a fixed order. Their versions and
governance values are included in the authorization basis and rechecked
immediately before receipt insertion. Patient-level scoped authority requires
an active, not-yet-discharged encounter; historical patient identity requires
an independently authorized global clinical-reader role. The exact
authorization basis and role snapshot are revalidated before values are read.
The service then inserts an
`IONE PHI Access Receipt` before returning data. Frappe commits the unsafe POST
transaction before the HTTP response is emitted. If receipt validation or
commit fails, no PHI response is returned.

The receipt is append-only, system-created, non-exportable, and contains no
patient name, source patient ID, encounter number, or raw record name. It
records the reader, purpose, policy checksum, field-manifest hash,
scope, authorization-basis hash, role-snapshot hash, HMAC key ID, session hash,
access time, and an HMAC reference. Reusing a request ID is rejected under an
advisory lock. Policy-bounded per-user and per-record rate limits return HTTP
429 before any identity value is returned.

The receipt's complete semantic payload is additionally authenticated with a
site-keyed HMAC in `record_hash`. Opening a receipt re-verifies that tag and
fails closed if any reader, purpose, policy, field, scope, session, time, or
result binding has changed.

Successful responses are marked `Cache-Control: no-store, private, max-age=0`,
`Pragma: no-cache`, `Expires: 0`, and `Vary: Cookie`. The Desk action escapes
every displayed value and does not log or persist the response.

## Patient/Encounter identity-key rotation

Identity-key rotation is a two-phase rolling configuration change. Do not
switch directly from an old-only fleet to a new-active fleet: an old worker
would be unable to find a row created only under the new key.

1. With the old key still active, add the future key ID/secret to
   `ione_identity_hmac_verify_keys`. Restart every web, queue, scheduler, and
   socket worker and prove the whole fleet sees both contracts.
2. Atomically change the active ID/secret to the future key and replace the
   ring entry with the prior active key. Roll the whole fleet again. Old-phase
   and new-phase workers now calculate and lock the same two candidate
   surrogates. New workers retain those locks through outer transaction
   commit/rollback; database-level unique constraints on an exact-case,
   length-delimited SHA-256 fingerprint of the complete stable source tuple are
   the final arbiter while an older worker is still draining.
3. Only after every worker is on phase 2, repeatedly run the bounded
   `backfill_site_identity_keys` maintenance call. Each pass captures one
   active target/ring snapshot, verifies the stored surrogate under its exact
   old contract, collision-locks the target, and atomically updates the key and
   contract. Continue until `has_more=false` and `blocked=0`, then rerun once
   and require `updated=0`.
4. Audit both Patient and Encounter indexes by `identity_key_contract`. Remove
   the old ring key only when every row uses the active contract, the
   cross-worker concurrency test has passed, and the rollback window has
   formally closed. Removing it earlier fails closed by making residual old
   rows cryptographically unverifiable. The exact-case stable fingerprint is
   still resolved under a transaction-held hashed lock, so the unavailable row
   blocks a duplicate insert; the raw identifier is never written to a lock
   name or log.

Normal ingestion dual-reads active and retained candidates and lazily rekeys
old rows. It also queries the exact-case `stable_source_key` independently of
the key ring and, for legacy transition only, performs a binary comparison of
the four raw tuple parts. Patient and Encounter indexes each enforce a unique
64-character stable fingerprint; the previous case-insensitive composite
index is removed after bounded verification. These are corruption guards, not
a substitute for the complete bounded migration and contract-count audit.

## Production activation gate

Keep identity disclosure disabled until all of the following exist:

- hospital-approved purpose codes, minimum field matrix, eligible business
  roles, organizational scopes, effective dates, and approval references;
- named users with least-privilege role assignments and no shared accounts;
- four distinct dedicated secrets and IDs configured before install:
  `ione_identity_hmac_key[_id]`, `ione_phi_hmac_key[_id]`, and
  `ione_sensitive_hash_hmac_key[_id]`, plus
  `ione_rule_receipt_hmac_key[_id]`; retained PHI and rule-receipt verification
  keys are recorded in `ione_phi_hmac_verify_keys` and
  `ione_rule_receipt_hmac_verify_keys`, and any in-progress identity rotation
  follows the two-phase `ione_identity_hmac_verify_keys` procedure above;
- privacy impact assessment and legal retention decision for access receipts;
- positive, negative, cross-hospital, cross-campus, cross-department,
  cross-ward, expired-policy, ambiguous-policy, duplicate-request, REST/list,
  report, export, link-search, and browser-cache UAT;
- monitoring for unusual access volume, after-hours access, policy changes,
  `Administrator` login, and receipt-integrity failures; and
- incident response and role-revocation procedures verified by the hospital.

Until these inputs are approved, production clinical activation remains
fail-closed even if the application itself is installed successfully.
