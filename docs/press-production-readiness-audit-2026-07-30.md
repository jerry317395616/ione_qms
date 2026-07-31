# Press production readiness audit — 2026-07-30

## Decision

`manager.myyr.top` is reachable and scheduled jobs are executing, but the
environment is **NO-GO for IONE QMS deployment**. This audit is evidence for a
release gate; it is not a deployment record or an acceptance result.

The inspection used an existing SSH key and read-only operating-system,
container, Git, Press database, Bench, HTTP, and model-health commands. It did
not fetch, pull, reset, prune, restart, build, install, migrate, back up,
restore, or execute a non-SELECT database statement. No password, private key,
private network address, token, or model response content is recorded here.

## Read-only follow-up — 2026-07-31

A later read-only check found that the root filesystem had been expanded or
otherwise remediated outside this deployment task: it now reports 3.7 TiB
total, approximately 2.8 TiB free, and 23% used. No image or build cache was
pruned by this task. The earlier 17 GiB headroom finding is retained below as
the state observed during the original audit, but it is no longer the current
capacity blocker. Build-time RAM, swap, image growth, and post-build free space
still require observation and release evidence.

Press has since moved `manager.myyr.top` to a manager-only active Bench,
`bench-0002-000044-f1`, on Release Group `bench-0002`; its active deploy
candidate is `deploy-0002-000044` and build is `ofanm7c9r7`. The only other
previously co-located site, `screening.myyr.top`, is Archived on old Bench
`bench-0002-000031-f1`. The manager candidate therefore no longer has a
cross-site Bench blast radius, but its complete 22-app immutable Bench App
release set must still be preserved while adding only IONE QMS.

The explicit `api.github.com` hosts override now terminates through the managed
local forwarder successfully: a read-only API request returned HTTP 200.
Authenticated App metadata access and private-repository fetch must still pass
for the exact IONE QMS fork before the App Source is accepted. Do not embed a
long-lived GitHub credential in a repository URL.

## Verified current state

- Press and its tunnel service are active. Public ping endpoints for Press and
  manager returned HTTP 200 on the successful IPv4 probes.
- `manager.myyr.top` is the only Active site on Bench
  `bench-0002-000044-f1`. `screening.myyr.top` is Archived on the prior Bench.
- The running application commits are:
  - Frappe: `5aef9956c7bf58d36cfd59e2242cc1a26d385ea2`
  - Flow: `4ac02293f656d9a979214c62b168512b5b6d008e`
  - Drive: `cd3438d1ab0b0fc1b8c10e282639ec0bd2ee7d82`
- The three Press App Sources point to the user's forks and the `develop`
  branch, with polling enabled and no recorded polling failure.
- The running Bench does not contain `ione_qms`. Press has no IONE QMS App
  Source, Release Group App, or Bench App record.
- Web, Socket.IO, two workers, and Redis are running. Supervisor reports its
  schedule process as `EXITED`, but current worker evidence proves that
  scheduled jobs are executing successfully. This is a supervision/monitoring
  anomaly to resolve and test, not evidence that scheduling is completely
  stopped.
- The model container is running and serves
  `qwen3.6-35b-a3b-fp8`. Local health succeeds.

## Blocking release gates

### Press topology and reproducibility

- Create an IONE QMS App Source from the private fork and bind the approved
  immutable release commit. A moving branch alone is not an acceptable
  production artifact.
- Preserve every exact current Bench App release. Generate the next Press
  candidate from the active manager-only Bench and add only the exact IONE QMS
  release; do not rebuild unrelated apps from moving `develop` tips.
- The Press control-plane checkout has 34 pre-existing changes. The running
  Flow checkout has an untracked lockfile and Drive has a modified generated
  declaration file. Do not clean or overwrite these files without an owner
  decision; produce a reproducible clean candidate instead.
- Verify the Press update transport and the abnormal supervisor SSH status
  before relying on automated rollback.

### Capacity

- The original audit observed 86% usage and approximately 17 GiB free. The
  current read-only follow-up observes 3.7 TiB total, approximately 2.8 TiB
  free, and 23% usage; disk capacity is no longer the current blocker.
- No cache was pruned by this task. Record disk/image growth before and after
  the ARM64 build.
- Available RAM and swap usage must be observed during the candidate build and
  migration; current memory pressure does not justify an unmonitored build.

### Backup and recovery

- The latest observed successful manager backup is from 2026-07-30 15:54 and
  includes files, but it is not offsite.
- There is no Backup Restoration Test or Physical Backup Restoration record.
- Scheduled logical and physical backup flags are both configured to skip.
- Before deployment, create a restorable database/private-files/public-files/
  configuration backup, copy it offsite, restore it into an isolated target,
  record RPO/RTO evidence, and prove rollback to the prior Press candidate.

### Qwen network and authentication

- The model port is bound to all interfaces and is reachable through a public
  hostname.
- Anonymous model discovery succeeds, and an explicitly invalid Bearer token
  also succeeds.
- Production requires an internal-only route (or a strictly authenticated
  gateway), a generated secret stored only in Flow's encrypted provider
  password field, anonymous and invalid-token requests returning 401/403,
  bounded request size/rate/timeouts, and the application readiness test
  passing from the manager Bench network.
- Embeddings remain unavailable; all RAG-dependent features must stay disabled
  until a separately approved embedding service and retrieval test set pass.

## Required deployment evidence

1. All repository static, metadata, JavaScript, dependency, secret, and Bench
   tests pass for the exact release commit and the exact pinned Frappe/Flow/
   Drive commits.
2. A clean ARM64 Press candidate is built from those immutable commits.
3. Backup, isolated restore, capacity, Qwen authentication, scheduler, worker,
   queue, and rollback gates above are green.
4. IONE QMS is added only through Press, then installed on manager through the
   Press Site application workflow. Do not run an ad-hoc production
   `bench get-app` or bypass Press state.
5. Post-install migrations, hooks, roles, scheduled jobs, Qwen readiness,
   negative permissions, smoke tests, and audit logs pass. The bounded worker
   must reach a hash-verified `IONE Migration Completion Receipt` bound to the
   current app version and deterministic source/DocType/migration-plan
   fingerprint, followed by an explicit readiness assertion; any
   Pending/Running/Blocked/Failed or stale receipt is a no-go. Do not enable
   unapproved clinical rules, indicators, agents, source writes, or RAG.
6. Hospital interface contracts, clinical definitions, rule gold samples,
   archive-ACK semantics, privacy/security review, performance/HA evidence,
   disaster recovery, and user acceptance are signed before production
   activation.

In-place uninstall is intentionally unavailable. First-install rollback must
restore the exact prior Press candidate together with its matched database,
configuration, public-file, and private-file snapshots.
Because Frappe commits the installed-app record before `after_install`, any
failure in that hook is treated as a partial installation and requires the
same whole-snapshot restore even though the hook's individual reconciliation
steps are idempotent.
