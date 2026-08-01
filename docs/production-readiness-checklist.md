# Production readiness checklist

Every item must have dated evidence and an owner.

## Release

- [ ] `ione_qms` release commit reviewed, signed/tagged as required, and pinned.
- [ ] Frappe, Flow, Drive, and every bench app commit pinned.
- [ ] The candidate reuses the current `manager` Press App Release/Bench App
      objects for Frappe `5aef9956c7bf58d36cfd59e2242cc1a26d385ea2`,
      Flow `4ac02293f656d9a979214c62b168512b5b6d008e`, and Drive
      `cd3438d1ab0b0fc1b8c10e282639ec0bd2ee7d82`; none was rebuilt from the
      moving `develop` branch.
- [ ] No project change exists in an official application checkout.
- [ ] Shared-bench site impact reviewed.
- [ ] Build logs, dependency inventory, and scans accepted.
- [ ] A native ARM64 Press QA candidate completes dependency installation,
      full asset build, migrate, Frappe/Flow/Drive/QMS tests, smoke tests, and
      rollback. The CI cross-platform Python-wheel preflight is not accepted
      as a substitute.

## Clinical and data

- [ ] Source inventory, mappings, owners, and read-only grants approved.
- [ ] Every enabled Endpoint references an effective, source/endpoint-bound
      Active Mapping version; no enabled Endpoint has a legacy inline scope
      mapping or `connector_options.master_data`.
- [ ] Mapping version switch and retained-message retry prove that Message/Event
      snapshots remain checksum-valid and neither scope nor Patient/Encounter
      projection mapping drifts to the new Mapping or later Endpoint options.
- [ ] Every enabled HL7 v2 Endpoint is exact-type HMAC inbound, has an approved
      charset/timezone/batch contract, and has passed raw-body signature,
      media-type, MLLP/ER7, duplicate/conflict, mapping, tenant, ACK/retry, and
      reconciliation acceptance with the hospital interface engine.
- [ ] Standards, clauses, and Rule/Indicator Versions have verified exact
      authority snapshots, mandatory non-empty SHA-256 hashes over attached
      private source-File bytes, contained
      effective intervals, and no mutable-parent fallback.
- [ ] URL-only legacy Standard Versions are quarantined; allowed remote
      authorities were archived through the controlled HTTPS host allowlist.
- [ ] Every rule has all seven required Test categories and passing immutable
      receipts for the exact current checksum and deployed evaluator commit;
      a mutation/stale-receipt negative test fails closed.
- [ ] Historical replay is accepted for the current checksum with non-empty
      coverage and zero errors.
- [ ] The online Shadow Run window has ended with its frozen minimum event and
      feedback counts/coverage, deterministic HMAC-selected Passed and Excluded
      strata, independent gold labels, no execution error/missing-data/unresolved
      review, and accepted false-positive and false-negative rates. Every
      execution, feedback, and replay receipt was canonically recomputed. No
      shadow result created a finding, alert, notification, or event error.
- [ ] Reconciliation has no unexplained gap.
- [ ] Data-quality thresholds accepted.

## Security and AI

- [ ] Role matrix and all negative scope tests pass.
- [ ] System Manager alone cannot read patient data.
- [ ] Incoming signatures, replay protection, CIDRs, and rate limits pass.
- [ ] Qwen uses an approved internal host and authentication.
- [ ] Anonymous model discovery returns 401/403.
- [ ] Global Flow/`ione_core` policy cannot bypass IONE write confirmation.
- [ ] Effective Flow API and `File` DocType override chains are captured and
      tested for the exact candidate.
- [ ] Known issues in the pinned Drive File controller are fixed upstream,
      upgraded, or explicitly accepted by the responsible security owner.
- [ ] Service users are non-Administrator and minimally scoped.
- [ ] Embedding/RAG is either fully accepted or feature-disabled.
- [ ] Prompt-injection, tool misuse, and log-redaction tests pass.
- [ ] Patient/Encounter identities use the dedicated stable identity HMAC key;
      PHI policy/receipt signatures and sensitive-value hashes use two separate
      versioned keys. No key falls back to `encryption_key`, historical PHI
      verification keys are retained, and identity rotation proves the
      two-phase overlapping ring, full-fleet restarts, bounded rekey with
      `has_more=false`/`blocked=0`, zero-update confirmation, and zero
      non-active contracts before old-key removal.
- [ ] Patient/Encounter exact-case, length-delimited `stable_source_key`
      fingerprints are fully backfilled and protected by their named unique
      constraints; the legacy case-insensitive composite indexes are absent,
      and case/delimiter collision plus old/new-worker commit-failure tests
      pass.
- [ ] Generic resource/filter/report/print/export/download routes, File URL or
      content-hash aliases, and Comment/Communication/Version/ToDo/Prepared
      Report sidecars cannot expose a registry-protected field, including in a
      technical Administrator session.
- [ ] Patient identity disclosure receipts bind and revalidate the exact locked
      clinical authorization basis and independently authorizing role; a
      multi-role confused-deputy negative test fails.
- [ ] Qwen medical-record input uses only the release-approved semantic profile,
      recursively removes identity keys/known values, fails closed on residual
      phone/ID/email/labeled identifiers, and stores only section hashes in the
      AI access log. Canary values are absent from prompt, tool output, Flow
      messages/run, drafts, candidates, incidents, and error logs.
- [ ] Aggregate-report outer output quarantine covers no-tool final answers,
      malformed/unknown tool calls, questions/errors, provider call IDs,
      confirmation resumes and orphan recovery with zero retained direct
      identifier content.
- [ ] Direct-dependency and installed-Bench Python audits pass; the final Press
      image SBOM additionally covers Python, Node, OS packages, and exact app
      SHAs with no unaccepted critical/high finding.
- [ ] Flow Session retention is at least the QMS maximum plus seven days.
- [ ] Missing/mismatched revision hashes fail closed without redaction, and an
      empty Flow output still redacts persisted assistant messages.
- [ ] Access snapshots redact only after terminal-task cutoff and successful
      content/record-hash checks; access receipts remain verifiable.
- [ ] Only terminal `Consumed`/`Expired` approvals redact after independent
      argument/prompt-hash checks; pending/resumable, legacy, and partial-marker
      records fail closed.
- [ ] Governed Flow attachments are rejected at creation/link/retention; every
      legacy attachment and its original File has an approved incident outcome.
- [ ] Frappe `Log Settings` cleanup preserves every unverified governed Flow
      Session beyond the safety window; ordinary Flow cleanup remains functional.
- [ ] The current Agent Evaluation suite contains the complete typed contract
      for accuracy, recall, false-positive, evidence consistency,
      hallucination, expert adoption, cross-scope tool overreach, forbidden
      tools, PII canaries, and missing evidence. Every case declares its
      structured output, citations/evidence, closed tool-argument schemas,
      expected denials, and namespaced synthetic fixtures.
- [ ] Agent Evaluation runs exercise the exact current Flow Agent, live IONE
      Tool callables/schemas, policy, scope, and evidence path in an explicitly
      configured rollback-only transaction. Commit attempts are rejected and
      post-rollback checks prove zero retained fixture, task, Flow session/run,
      access-log, draft, candidate, or formal-artifact residue. If isolation
      cannot be proven, the production gate fails closed; a dedicated test site
      is accepted only for QA and never authorizes production.
- [ ] Evaluation constructs Flow Agents with `auto_approve=False`. Releases
      containing confirmation tools are rejected in rollback-only mode and
      remain production-blocked until a dedicated synthetic site proves the
      committed IONE pause, independent review, and resume path under a
      separately approved receipt-promotion design.
- [ ] The independently approved, versioned evaluation threshold policy has a
      valid site-keyed signature. Governed confusion matrices meet every
      accuracy/recall/false-positive/evidence-consistency/hallucination/expert-
      adoption threshold, while cross-scope overreach, forbidden tools, PII
      leakage, and missing evidence remain exactly zero.
- [ ] The newest evaluation for the current binding is Passed and independently
      Approved, and its immutable receipt re-verifies the exact Agent Release,
      model/configuration, prompts/instructions, live tool schemas, policy,
      knowledge-base manifest, suite, threshold policy, evaluator code, category
      matrix, and receipt hash. Any newer pending/failed receipt or any drift
      blocks production.
- [ ] Adversarial evaluation proves malformed/extra output, nonexistent or
      cross-task citations, schema-invalid/cross-scope tool arguments, forbidden
      calls, PII canaries, missing evidence, stale bindings, and attempted
      transaction commits all fail closed.
- [ ] Generic Desk/import/REST CRUD, including technical Administrator access,
      cannot create, mutate, or delete an `IONE AI Analysis Task`.

## Reliability and operations

- [ ] Scheduler and all worker queues run and are monitored.
- [ ] Install preflight proves every reserved Role, Flow Tool/Agent/Model, and
      service-user, Workflow, and reserved-agent Trigger name is absent before
      any IONE schema is synchronized; reused Workflow State/Action and Flow
      retention records match their non-mutated shared contracts.
- [ ] `IONE Migration State` is `Completed` for the candidate schema revision;
      its linked append-only `IONE Migration Completion Receipt`, canonical
      summary, batch count, completion timestamp, current app version, complete
      source/DocType/plan fingerprint, and record hash all verify.
- [ ] No bounded migration remains `Pending`, `Running`, `Blocked`, or `Failed`;
      multi-batch continuation and hourly repair scheduling have been observed.
- [ ] Durable indicator/analytics runs prove deterministic run/work identities,
      ordered source-epoch barriers, double-scan/final verification, per-page
      commits, stale-fact cleanup, eight-attempt retry/dead-letter behavior,
      lost-enqueue/stale-lease recovery, and duplicate-worker exclusion.
- [ ] Dead-letter recovery rejects Guest, technical Administrator, and agent
      service identities; a named authorized human POST appends an immutable
      reason/actor/time/sequence receipt without changing run generation.
- [ ] The `definition_lineage` migration reports `has_more=false` and
      `blocked=0`; every retained legacy ambiguity has an append-only lineage
      receipt and a reviewed retirement/replacement outcome.
- [ ] Endpoint cursors are restricted and Integration Job audits expose only
      cursor SHA-256 fingerprints; config/operator CRUD cannot rewrite jobs.
- [ ] AI queue, RQ/model timeouts, 30-minute lease, heartbeat renewal, and
      expired-Resume behavior pass crash/recovery tests.
- [ ] Monthly report task/snapshot/recovery receipt and schedule runtime
      pointer commit atomically before enqueue; injected save/commit/Redis
      failures neither skip a month nor execute a rolled-back task, and the
      Pending-task repair sweep is monitored.
- [ ] Capacity and disk headroom meet policy.
- [ ] Load and recovery tests meet latency/throughput/error targets.
- [ ] Database, configuration, public files, and private files are backed up.
- [ ] Offsite backup is current.
- [ ] Isolated restore meets RPO/RTO.
- [ ] Prior Press candidate rollback succeeds.
- [ ] Rollback restores the prior Press candidate plus verified database,
      public-file, and private-file snapshots; `bench uninstall-app` is not
      used and is intentionally rejected by the application.
- [ ] Alerts, on-call ownership, change window, and communications are ready.

## Deployment

- [ ] One `IONE Production Readiness Assessment` contains exactly all 14 P0
      gate codes, a private immutable manifest whose file and artifact hashes
      reverify, the exact deployed Release Record/app commits/schema/migration
      receipt, named evidence owners, and an unexpired validity window.
- [ ] A named `IONE QMS Auditor`, independent of the assessor, approved the
      exact assessment checksum; a third named operator activates it through
      the POST-only governed API. Direct settings writes, stale/tampered
      evidence, and actor reuse all fail closed.
- [ ] Activation, deactivation, and revocation append hash-verifiable events;
      emergency deactivation atomically clears production, real-time rules,
      and AI.

- [ ] QA candidate matches production app combination.
- [ ] If `manager` shares a bench, Press splits only `manager`; every other site
      remains on its recorded prior candidate unless explicitly approved.
- [ ] Migrations and rollback compatibility are reviewed.
- [ ] Production integrations, rules, AI, and triggers default disabled.
- [ ] Integration, real-time rule, AI, and production-mode switches remain
      fail-closed until the current migration completion receipt verifies.
- [ ] Every non-migration scheduler/worker entry point rejects execution until
      the current receipt verifies; CI calls `assert_migration_complete` after
      the bounded worker and treats any rejection as a failed job.
- [ ] An injected `after_install` failure is recovered only by restoring the
      complete pre-install Press snapshot; installed-app metadata is never
      edited and generic uninstall is never used.
- [ ] Manager and all sites sharing the bench pass smoke tests.
- [ ] Observation window completes without a release-blocking incident.
