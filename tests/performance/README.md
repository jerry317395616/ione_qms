# Production-equivalent performance harness

These k6 workloads measure the two explicit V1.0 capacity paths:

- signed clinical-event intake and deterministic-rule throughput;
- authenticated command-center usage at up to 1,000 concurrent users.

They are evidence generators, not proof by themselves. A signed report must
also record the exact Frappe, Flow, and IONE QMS commit SHAs; Press candidate;
database size; worker topology; hardware; warm-up; duration; error and latency
percentiles; queue depth; database/Redis/Qwen resource graphs; and all
restarts or faults injected during the run.

## Safety boundary

The scripts refuse to start unless both conditions are explicit:

1. `ALLOW_LOAD_TEST=YES`
2. `TARGET_ENV` is `qa`, `staging`, or `performance`

Do not point them at `manager` or any shared production bench. Use synthetic
patients and isolated source endpoints only. Keep API tokens and signing
secrets outside the repository, terminal history, logs, and generated reports.

## Signed ingestion

At 60 events/second the workload exceeds the 5-million-events/day equivalent
rate. With `RULES_PER_EVENT=2`, it also exceeds the
10-million-rule-evaluations/day equivalent rate. The QA endpoint must be
configured with a matching HMAC secret, a suitable rate limit, and a
synthetic-only mapping.

The script runs one signed schema preflight before starting the arrival-rate
scenario. A non-2xx/expected-idempotency response aborts the test, so a route,
authentication, or envelope mismatch cannot produce a misleading throughput
report.

```bash
k6 run \
  -e ALLOW_LOAD_TEST=YES \
  -e TARGET_ENV=performance \
  -e BASE_URL=https://qms-perf.example.internal \
  -e IONE_ENDPOINT=PERF-SYNTHETIC \
  -e IONE_SHARED_SECRET=... \
  -e EVENT_RATE=60 \
  -e RULES_PER_EVENT=2 \
  -e TEST_DURATION=30m \
  tests/performance/k6-ingestion.js
```

Required gates are at least 99.5% accepted requests, no dropped iterations,
and request p95 at or below three seconds. Separately verify that workers
produce the expected event, execution, and finding counts without duplicates
or loss.

## Command center

`AUTH_TOKENS` is a comma-separated set of `api_key:api_secret` pairs belonging
to synthetic named users. Provide multiple hospital/department-scoped users
and verify both positive and negative scope results; one privileged token does
not constitute a role-matrix test.

```bash
k6 run \
  -e ALLOW_LOAD_TEST=YES \
  -e TARGET_ENV=performance \
  -e BASE_URL=https://qms-perf.example.internal \
  -e AUTH_TOKENS=key1:secret1,key2:secret2 \
  -e DEPARTMENTS=PERF-DEPT-01,PERF-DEPT-02 \
  -e USER_CONCURRENCY=1000 \
  -e TEST_DURATION=30m \
  tests/performance/k6-command-center.js
```

The normal-page acceptance gate is p95 at or below two seconds. The test is a
no-go if cross-scope data appears, request failures reach 1%, workers or
scheduler become unhealthy, queues fail to drain, or resource saturation
persists after the observation window.

## Recovery evidence

During a separate approved run, restart one worker at a time and prove that:

- no event, execution, indicator result, or finding is lost or duplicated;
- retry and dead-letter queues remain observable and drain;
- the scheduler remains singleton and resumes due work;
- the prior Press candidate can be restored;
- an offsite database-and-files backup restores inside the approved RPO/RTO.

Store sanitized k6 summaries and signed evidence in the controlled release
record, not in this source repository.
