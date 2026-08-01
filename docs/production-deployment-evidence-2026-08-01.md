# IONE QMS production deployment evidence — 2026-08-01

## Decision

The application implementation and Press installation baseline are operational
on `manager.myyr.top`. Production clinical activation remains **NO-GO**.

This distinction is intentional. The software, schema, deterministic engines,
governed AI integration, migrations, and rollback path are present and tested.
The hospital has not supplied the real organization master data, source-system
contracts, approved clinical definitions, gold cases, operational sign-offs,
or offsite-storage credentials that would authorize real-time rules, AI
triggers, or production mode. Those switches therefore remain fail-closed.

This report supersedes runtime statements in the 2026-07-29 static gap audit
and the 2026-07-30 read-only Press audit. Those files remain historical records.

Snapshot boundary: this repository document records candidate 53 before the
offline evidence-manifest generator was added. It is intentionally historical,
because a Git commit cannot contain its own final SHA. Each promoted successor
must store its exact CI, SBOM, Press, backup, migration, runtime, and all-app-SHA
evidence in the external checksum-bound release package and `IONE Release
Record`, not by rewriting this snapshot as self-attestation.

## Immutable release and source controls

- Site: `manager.myyr.top`, Active on Bench `bench-0002-000053-f1`.
- Deploy Candidate: `deploy-0002-000053`; ARM64 full build `hd2tmrrpib`,
  Success.
- Installed IONE baseline at the time of this audit:
  `a3b39206947e0e1b0dd079ef3f599946e0101737` from App Release `tkhnm0408m`.
- Candidate comparison against `deploy-0002-000052` proves that only
  `ione_qms` changed. Every other app retained its exact App Release and hash.
- The 16 Frappe-ecosystem official forks used by the candidate point to the
  user's GitHub organization and use `develop` as both the GitHub default
  branch and the Press App Source branch. The release remains pinned even when
  the moving branch advances.
- The five pre-existing non-official IONE/business applications retain their
  existing `main` or `version-17` branch contracts. They are not rewritten by
  this project.
- No Frappe, Flow, Drive, Helpdesk, ERPNext, or other official application
  source checkout is edited by IONE QMS.

## Source, schema, and automated verification

- Metadata validator: 127 DocTypes and 9 modules, passed.
- Dependency-free source test suite: 295 tests, passed for the deployed
  production-gate commit.
- Python compilation includes both application code and release tools.
- GitHub Actions Run `30694206135` for the deployed commit completed every
  required job successfully; local results never substitute for that pinned
  workflow and Press build.
- The manager site reports 21 installed applications including `ione_qms` and
  Flow. Installation and two consecutive migrations completed successfully.
- The hash-bound `IONE Migration Completion Receipt` verifies the schema,
  source/DocType/migration-plan fingerprint, and current app version.
- Application cryptographic configuration, patient-identity key separation,
  PHI query boundary, Flow tool integrity, roles, service-user isolation, and
  migration gate all pass runtime acceptance.

## Blueprint requirement coverage

| Section | Requirement | Current authoritative evidence | Decision |
| --- | --- | --- | --- |
| 0 | Single custom app, deterministic-first quality control, governed AI | One `ione_qms` app, nine modules; AI writes only drafts/candidates | Implemented and runtime-verified |
| 1 | Hospital-wide standards, monitoring, finding, rectification, PDCA, analytics and AI foundation | 124 source DocTypes, 9 Workspaces, 8 Workflows, 5 governed Agent blueprints | Platform implemented; hospital content blocked |
| 2 | Policy governance and 2026 ten-goal topics | Draft national-goal Standard, 10 clauses, 10 indicator templates and 10 representative deterministic rule templates | Implemented fail-closed; clinical approval blocked |
| 3 | Modular monolith, evidence lineage, data minimization and read-only sources | Module boundaries, services, immutable evidence/version receipts, connector write prohibition | Implemented and verified by contracts |
| 4 | Frappe/Flow develop baseline with production SHA pinning | User forks/default branches audited; candidate hashes frozen | Verified |
| 5 | Layered access, domain, integration, AI and analytics architecture | Runtime schema, APIs, workers, Qwen relay and Desk endpoints available | Implemented; real source chain blocked |
| 6 | Nine application modules | Nine module directories and Workspaces migrated | Verified |
| 7 | Foundation, standard/rule/indicator, finding and AI models | Source metadata passes and installed schema contains the IONE models | Verified |
| 8 | Standards, rules, running/final record, surgery, safety events, PDCA, workbenches | Domain services, pages, reports, Workflows and negative-path tests are installed | Software implemented; role/clinical UAT blocked |
| 9 | Deterministic rule engine, lifecycle, five outcomes, evidence and shadow safeguards | Safe condition evaluator, plugin registry, seven-category tests, replay/shadow receipts, immutable evidence | Implemented; published hospital rules absent |
| 10 | Versioned indicators, nine dimensions, append-only facts and lineage | Indicator registry, versions, source receipts, revision pointers, facts and reports | Implemented; approved formula/source mappings absent |
| 11 | Oracle/API/HL7/FHIR/file integration, idempotency, retry and reconciliation | Read-only connector contracts, signed HL7 HTTP adapter, mapping snapshots, retry/dead-letter and source reconciliation | Implemented; real endpoints/queries absent |
| 12 | Five Flow Agents, least-privilege tools, triggers, policies and evaluation | Authenticated private Qwen readiness passes; 5 Agents disabled, 5 Policies Draft, zero governed triggers enabled | Runtime-ready but activation blocked |
| 13 | Role, scope, privacy, export, audit and AI data controls | 16 IONE roles, service users with one minimal role/no API key, PHI and export guards | Implemented; hospital SSO/MFA/UAT blocked |
| 14 | Capacity, queues, degradation, 99.9%, RPO/RTO and load targets | Queue recovery verified; isolated restore RTO 143.818 seconds | Capacity, load/HA and RPO remain NO-GO |
| 15 | Controlled environments and pinned container deployment | Press ARM64 image, candidate, site install, migration and rollback evidence | Deployment verified; offsite/DR incomplete |
| 16 | Controller/service boundaries and hooks | Hook targets validate under the required Python 3.14 runtime and manager scheduler/workers run | Verified |
| 17 | Develop workflow, immutable release and CI/CD | Default branches, pinned releases, green CI and Press build verified | Verified |
| 18 | Unit, integration, permission, AI, performance and disaster tests | 295 source tests plus runtime/restore checks | Clinical, load, HA and signed UAT remain external |
| 19 | Phased rollout and pilot departments | Platform phase is installed | Pilot scope and exit criteria not supplied |
| 20 | Governance organization, staffing and budget | No software substitute is valid | Hospital decision/sign-off required |
| 21 | Functional, data/rule and management-effect acceptance | Software acceptance evidence exists; real-data KPIs and 3–6 month outcomes do not | Formal acceptance NO-GO |
| 22 | Risks: moving develop, bad data, false positives, Oracle load, AI abuse, rollback | Pinned hashes, fail-closed switches, replay/shadow, tool allowlists and restore procedure | Technical controls verified; operational sign-off blocked |
| 23 | Traceable end-to-end quality and AI decisions | Immutable definition/evidence/execution/approval receipts are implemented | Requires real hospital records and signed UAT |

## Runtime acceptance evidence

- HTTPS `/`, `/app`, `/helpdesk`, the IONE Quality Command Center route, and
  `/api/method/ping` complete with HTTP 200 and valid TLS.
- HSTS, `nosniff`, and `SAMEORIGIN` response protections are present.
- The manager container is running with zero restarts and no OOM kill; two
  Bench workers are online.
- The Qwen Flow Model uses the approved exact model ID through an authenticated
  private relay. Anonymous operation is not accepted by the readiness check.
- Production mode, realtime rules, system AI, IONE AI, governed Agents, and
  governed Flow Triggers all remain disabled.
- Helpdesk SQLite FTS integrity is `ok`; source/index counts match. Its legacy
  RediSearch index also matches its source records. No new search error or
  failed scheduled-job log was produced by the acceptance run.
- The final Press short/default/long/sync queues ended empty with no failed,
  started, or deferred jobs. Build-only temporary egress was removed and the
  platform Docker proxy configuration was restored after the successful build.

- The dedicated production HMAC key family is configured independently of all
  other IONE keys. Runtime adversarial acceptance proves that direct production
  setting writes and forged activation-event inserts fail closed. There are no
  readiness assessments or activation events, and all production/real-time/AI
  switches remain disabled.
- Production activation requires all 14 exact evidence domains, a private
  byte-verified manifest, a current release/schema/migration/all-app-SHA
  binding, and three distinct named actors. The repository revision following
  this snapshot adds an offline hash-only manifest generator and a
  non-production register example; these do not replace hospital evidence or
  approval, and their eventual Press promotion needs a new external evidence
  package.

Operational changes enabling that result are configuration-only: the official
RedisSearch module shipped in the image is loaded through Redis configuration,
and Press worker/scheduler processes bypass the broken external proxy only for
GitHub hosts. Official application source files remain untouched.

## Backup and restore evidence

- The current full local Press backup includes database, site configuration,
  public files, and private files.
- The immediate pre-update backup is `219qvarkv2`; its Press Agent job and all
  four backup artifacts verified successfully.
- An isolated non-routable temporary site was restored on the exact current
  Bench. Scheduler, email, DNS, and proxy exposure remained disabled during the
  exercise.
- Restore plus verification took 143.818 seconds. Installed apps, 1,610 schema
  tables, 201 installed IONE DocTypes, record counts, roles, file inventory,
  migration receipt, cryptographic configuration, PHI boundary, Flow tool
  integrity, switches, and key fingerprints matched production.
- The temporary site, database, archive, and extracted material were removed
  after verification.
- The observed recovery point was 2,170.249 seconds old at drill start. RTO is
  inside the two-hour target, but this observation does not meet the 15-minute
  RPO target.
- No current independent object-storage bucket is configured. A local backup
  and an old offsite record do not satisfy the current offsite-backup gate.

## Open production activation gates

1. Configure an independently administered S3/OSS-compatible bucket, region or
   endpoint, access policy, encryption, retention, and credentials in Press.
   Create a current offsite backup and repeat an isolated restore with a
   recovery point no older than 15 minutes.
2. Supply signed hospital/campus/department/ward and staff master data;
   source-system/API/Oracle contracts, read-only grants, mappings and
   reconciliation queries; and notification/escalation ownership.
3. Supply approved standards, exact rule/indicator definitions, all required
   gold cases, replay/shadow results, false-positive/negative thresholds,
   meeting/action governance, and clinical/privacy/security sign-offs.
4. Complete representative 1,000-user, 5-million-event/day and
   10-million-rule/day testing; Web/worker/Redis/MariaDB/storage failure
   exercises; monitoring/on-call acceptance; SSO/MFA; vulnerability and
   penetration testing; and signed role/browser UAT.
5. Create the immutable deployed `IONE Release Record` under a named real
   deployer, generate the private evidence manifest from signed artifacts, and
   complete the required named assessor/auditor/independent-activator flow.
6. Replace weak administrative credentials, adopt managed key-only access
   where approved, and record credential rotation and break-glass ownership.

No sample value, guessed rule, or technical Administrator action may be used to
close these gates. They require the named hospital, clinical, security, and
operations owners defined in `hospital-production-inputs.md`.
