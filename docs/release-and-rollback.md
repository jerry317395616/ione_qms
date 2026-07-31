# Press release, deployment, and rollback

## Release invariants

- `ione_qms` uses its private GitHub repository and `develop` default branch.
- Official Frappe applications are not modified by this project.
- A production release records immutable commit SHAs even when App Sources
  track `develop`.
- The existing `manager` dependency baseline is an immutable Press snapshot:
  Frappe `5aef9956c7bf58d36cfd59e2242cc1a26d385ea2`, Flow
  `4ac02293f656d9a979214c62b168512b5b6d008e`, and Drive
  `cd3438d1ab0b0fc1b8c10e282639ec0bd2ee7d82`. Reuse those exact App
  Release/Bench App objects in the IONE candidate. Do not create replacement
  dependency releases from the current tip of `develop`: the repository
  default branch and an already-built Press snapshot are different release
  identities.
- The exact app combination is tested together. Updating the Release Group
  must not accidentally combine IONE installation with unrelated upstream
  upgrades.
- Sites sharing one bench are part of the same blast radius.
- The target host is ARM64. An x86_64 CI build or cross-resolved ARM64 Python
  wheel is evidence, but only a native ARM64 Press candidate can satisfy the
  production architecture gate.
- Migration identity is derived automatically from the application version,
  normalized shipped `ione_qms/**/*.py`, canonical
  `ione_qms/**/doctype/**/*.json`, and the ordered migration-plan manifest.
  The full SHA-256 fingerprint is stored in both migration state and receipt;
  changing any input invalidates an older receipt without relying on a manual
  revision bump.

## Staging sequence

1. Record the active candidate, image, app list, commits, database version,
   Python/Node versions, and site configuration hashes.
2. Prove the Press GitHub App has least-privilege access to the private
   `ione_qms` repository, then create an App Source and release at the reviewed
   commit.
3. Create a staging Release Group/candidate by reusing the exact current
   `manager` Frappe/Flow/Drive App Release objects and adding only the reviewed
   immutable `ione_qms` release.
4. Build the image natively on ARM64 and retain build logs, the complete
   app/runtime manifest, and an image-level SBOM.
5. Install to an isolated QA site through Press.
6. Run migrate, smoke, contract, role matrix, clinical sample, integration,
   Flow/Qwen, load, backup/restore, and rollback tests.
7. Obtain clinical, security, operations, and product acceptance.

## Production sequence

1. Confirm all P0/P1 gates and approved change window.
2. Verify scheduler/workers and at least 20% free capacity.
3. Create and verify database, config, public-file, private-file, and offsite
   backups; complete an isolated restore rehearsal.
4. Save the prior Press candidate/image and rollback instructions.
5. Assert the candidate changes only `ione_qms`; every other app source,
   release, hash, architecture, and the pinned Frappe/Flow commits must remain
   unchanged.
6. If `manager` shares its active bench, use Press to split only `manager` and
   prove every other site remains on the prior bench.
7. Capture the effective `File` controller MRO and permission hooks. With
   Drive installed, both the QMS mixin and Drive controller must be present and
   their file lifecycle/attachment protections must pass.
8. Deploy the saved candidate through Press.
9. Install `ione_qms` on `manager` through the Press Site App workflow if not
   already installed.
   The read-only `before_install` preflight rejects every unknown record using
   an IONE-owned Role, Workflow, Flow Tool/Agent/Model, service-user, or
   reserved-agent Trigger identity before schema synchronization. Shared
   Workflow States/Actions and Flow log-retention configuration are reused
   only after their canonical fields and structure validate.
10. Run migrate through the normal deployment lifecycle. Confirm the bounded
    long-queue migration continues across batches until `IONE Migration State`
    is `Completed` and its linked immutable completion receipt verifies the
    current full fingerprint and app version. CI and Press acceptance must
    explicitly execute `assert_migration_complete`; a `Pending`, `Running`,
    `Blocked`, `Failed`, missing, stale-version, stale-fingerprint, or
    hash-invalid receipt is a release blocker.
11. Keep integrations, published clinical rules, AI, production mode, and Flow
    triggers disabled until the migration receipt and their individual
    acceptance gates pass. Runtime validators reject early enablement.
12. Run smoke and negative permission tests on manager and every site sharing
    the bench.
13. Monitor errors, latency, queues, database, model, and clinical processing
    through the observation window.

## Rollback

In-place `bench uninstall-app ione_qms` is never a rollback mechanism and is
rejected by the application. IONE creates governed external Flow artifacts,
service identities, and append-only audit records that Frappe's generic
uninstaller cannot safely reverse.

Frappe runs `before_install` before IONE schema synchronization, so a preflight
collision is a zero-write rejection. After schema synchronization Frappe
records the app as installed and commits before invoking `after_install`.
Therefore an `after_install` exception is a partially installed Press
candidate even though every reconciliation step is safe to invoke again.
Production recovery must restore the matched pre-install database/config/files
snapshot and prior candidate; do not clear `installed_apps`, retry ad hoc, or
attempt generic uninstall.

For an upgrade of an already-installed site, application-only candidate
rollback is allowed only when the reviewed migration is backward compatible.
For first installation or any incompatible schema/data change, restore the
whole pre-deployment state:

1. Disable ingress/feature flags and preserve incident evidence.
2. Re-deploy the saved prior Press candidate; never reconstruct it from branch
   names or current repository heads.
3. Restore the matched pre-deployment database, site configuration, public
   files, and private files. This step is mandatory when rolling back first
   installation and whenever schema/data changes are not backward compatible.
4. Verify encryption key, permissions, representative records, integrations,
   scheduler, workers, and both shared-bench sites.
5. Reconcile messages and external-source watermarks before resuming.

Do not use `git reset --hard`, edit a running container, switch official app
branches, or manually patch database schema as rollback mechanisms.

## Release evidence

Store sanitized evidence under `docs/releases/<release-id>/`:

- release manifest and commits;
- dependency lock/SBOM and scans;
- migration plan/results;
- test and load reports;
- clinical approval references;
- backup and restore report;
- candidate/image identifiers;
- deployment timestamps and operator;
- smoke results;
- rollback rehearsal and observation outcome.

No passwords, tokens, private keys, full clinical records, or unredacted
screenshots belong in release evidence.
