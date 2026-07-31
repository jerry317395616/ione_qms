import crypto from "k6/crypto";
import http from "k6/http";
import {check, fail} from "k6";
import exec from "k6/execution";

const baseUrl = String(__ENV.BASE_URL || "").replace(/\/+$/, "");
const endpoint = String(__ENV.IONE_ENDPOINT || "");
const sharedSecret = String(__ENV.IONE_SHARED_SECRET || "");
const rate = Number(__ENV.EVENT_RATE || 60);
const duration = String(__ENV.TEST_DURATION || "15m");
const preAllocatedVUs = Number(__ENV.PREALLOCATED_VUS || 200);
const maxVUs = Number(__ENV.MAX_VUS || 1200);
const rulesPerEvent = Number(__ENV.RULES_PER_EVENT || 2);

if (__ENV.ALLOW_LOAD_TEST !== "YES") {
	fail("Set ALLOW_LOAD_TEST=YES only for an approved isolated QA load test.");
}
if (!["qa", "staging", "performance"].includes(String(__ENV.TARGET_ENV || "").toLowerCase())) {
	fail("TARGET_ENV must be qa, staging, or performance; the harness refuses production by default.");
}
if (!baseUrl || !endpoint || !sharedSecret) {
	fail("BASE_URL, IONE_ENDPOINT, and IONE_SHARED_SECRET are required.");
}
if (!Number.isFinite(rate) || rate < 1 || rate > 100000) {
	fail("EVENT_RATE must be between 1 and 100000 requests per second.");
}
if (!Number.isFinite(rulesPerEvent) || rulesPerEvent < 1 || rulesPerEvent > 10000) {
	fail("RULES_PER_EVENT must be between 1 and 10000.");
}

export const options = {
	discardResponseBodies: true,
	scenarios: {
		signed_event_ingestion: {
			executor: "constant-arrival-rate",
			rate,
			timeUnit: "1s",
			duration,
			preAllocatedVUs,
			maxVUs,
			gracefulStop: "2m",
			tags: {workload: "signed-event-ingestion"},
		},
	},
	thresholds: {
		checks: ["rate>=0.995"],
		http_req_failed: ["rate<0.005"],
		http_req_duration: ["p(95)<=3000"],
		dropped_iterations: ["count==0"],
	},
};

function syntheticEvent() {
	const timestamp = Date.now();
	const identity = `${exec.vu.idInTest}-${exec.scenario.iterationInTest}-${timestamp}`;
	return {
		event_type: "PerformanceSyntheticObservation",
		source_record_type: "SyntheticEncounter",
		source_record_id: `PERF-${identity}`,
		source_version: "1",
		event_time: new Date(timestamp).toISOString(),
		hospital_code: "PERF-HOSPITAL",
		department_code: `PERF-DEPT-${exec.vu.idInTest % 20}`,
		patient_external_id: `PERF-PATIENT-${identity}`,
		encounter_external_id: `PERF-ENCOUNTER-${identity}`,
		payload: {
			synthetic: true,
			rules_expected: rulesPerEvent,
			observation_code: "PERF-QMS",
			observation_value: exec.scenario.iterationInTest % 10,
		},
	};
}

function signedPost(event, nonceScope, tags) {
	const body = JSON.stringify(event);
	const timestamp = String(Math.floor(Date.now() / 1000));
	const nonce = crypto.sha256(
		`${nonceScope}:${Date.now()}:${Math.random()}`,
		"hex"
	);
	const signature = crypto.hmac(
		"sha256",
		sharedSecret,
		`${timestamp}.${nonce}.${endpoint}.${body}`,
		"hex"
	);
	return http.post(
		`${baseUrl}/api/method/ione_qms.api.integration.receive_event`,
		body,
		{
			headers: {
				"Content-Type": "application/json",
				"X-IONE-Endpoint": endpoint,
				"X-IONE-Timestamp": timestamp,
				"X-IONE-Nonce": nonce,
				"X-IONE-Signature": signature,
				"X-IONE-Synthetic": "1",
			},
			tags,
			timeout: "10s",
		}
	);
}

export function setup() {
	const now = Date.now();
	const response = signedPost(
		{
			event_type: "PerformanceSyntheticPreflight",
			event_time: new Date(now).toISOString(),
			source_record_type: "SyntheticEncounter",
			source_record_id: `PERF-PREFLIGHT-${now}`,
			source_version: "1",
			payload: {synthetic: true, preflight: true},
		},
		`preflight:${now}`,
		{endpoint: "receive_event", phase: "preflight"}
	);
	if (![200, 202, 409].includes(response.status)) {
		fail(`Signed ingestion preflight failed with HTTP ${response.status}; load phase was not started.`);
	}
}

export default function () {
	const response = signedPost(
		syntheticEvent(),
		`${exec.vu.idInTest}:${exec.scenario.iterationInTest}`,
		{endpoint: "receive_event", phase: "load"}
	);
	check(response, {
		"event accepted or idempotently replayed": (result) =>
			result.status === 200 || result.status === 202 || result.status === 409,
	});
}
