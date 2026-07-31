import http from "k6/http";
import {check, fail, sleep} from "k6";
import exec from "k6/execution";

const baseUrl = String(__ENV.BASE_URL || "").replace(/\/+$/, "");
const duration = String(__ENV.TEST_DURATION || "15m");
const concurrency = Number(__ENV.USER_CONCURRENCY || 1000);
const thinkTimeSeconds = Number(__ENV.THINK_TIME_SECONDS || 3);
const tokens = String(__ENV.AUTH_TOKENS || "")
	.split(",")
	.map((token) => token.trim())
	.filter(Boolean);
const departments = String(__ENV.DEPARTMENTS || "")
	.split(",")
	.map((department) => department.trim())
	.filter(Boolean);

if (__ENV.ALLOW_LOAD_TEST !== "YES") {
	fail("Set ALLOW_LOAD_TEST=YES only for an approved isolated QA load test.");
}
if (!["qa", "staging", "performance"].includes(String(__ENV.TARGET_ENV || "").toLowerCase())) {
	fail("TARGET_ENV must be qa, staging, or performance; the harness refuses production by default.");
}
if (!baseUrl || !tokens.length) {
	fail("BASE_URL and one or more comma-separated AUTH_TOKENS are required.");
}
if (!Number.isFinite(concurrency) || concurrency < 1 || concurrency > 10000) {
	fail("USER_CONCURRENCY must be between 1 and 10000.");
}

export const options = {
	discardResponseBodies: true,
	scenarios: {
		command_center_users: {
			executor: "constant-vus",
			vus: concurrency,
			duration,
			gracefulStop: "2m",
			tags: {workload: "command-center"},
		},
	},
	thresholds: {
		checks: ["rate>=0.99"],
		http_req_failed: ["rate<0.01"],
		"http_req_duration{endpoint:command_center}": ["p(95)<=2000"],
	},
};

function tokenForVu() {
	return tokens[(exec.vu.idInTest - 1) % tokens.length];
}

function departmentForIteration() {
	if (!departments.length) return "";
	return departments[exec.scenario.iterationInTest % departments.length];
}

export default function () {
	const department = departmentForIteration();
	const query = new URLSearchParams({days: "30"});
	if (department) query.set("department", department);
	const response = http.get(
		`${baseUrl}/api/method/ione_qms.api.dashboard.get_command_center?${query.toString()}`,
		{
			headers: {
				Authorization: `token ${tokenForVu()}`,
				Accept: "application/json",
				"X-IONE-Synthetic": "1",
			},
			tags: {endpoint: "command_center"},
			timeout: "10s",
		}
	);
	check(response, {
		"command center is authorized and available": (result) => result.status === 200,
	});
	sleep(thinkTimeSeconds);
}
