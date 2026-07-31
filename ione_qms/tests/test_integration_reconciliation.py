from __future__ import annotations

import json
import sys
from contextlib import nullcontext
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from ione_qms.integration import builtin_connectors
from ione_qms.integration.connectors import ReconciliationSnapshot
from ione_qms.tasks import integration


class Record(SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)

	def get_password(self, fieldname, raise_exception=False):
		del fieldname, raise_exception
		return "reviewed-password"


class TestReconciliationSnapshot(TestCase):
	def test_source_count_is_strictly_non_negative_integer(self) -> None:
		self.assertEqual(ReconciliationSnapshot(12, "abc").source_count, 12)
		for value in (-1, True, 1.5, "1"):
			with self.assertRaises((TypeError, ValueError)):
				ReconciliationSnapshot(value)  # type: ignore[arg-type]

	def test_details_and_hash_are_bounded_contract_values(self) -> None:
		with self.assertRaises(TypeError):
			ReconciliationSnapshot(1, details=[])  # type: ignore[arg-type]
		with self.assertRaises(ValueError):
			ReconciliationSnapshot(1, source_hash="x" * 65)
		with self.assertRaises(TypeError):
			ReconciliationSnapshot(1, source_hash=123)  # type: ignore[arg-type]
		with self.assertRaises(ValueError):
			ReconciliationSnapshot(1, details={"patient_id": "must-not-be-recorded"})
		with self.assertRaises(ValueError):
			ReconciliationSnapshot(1, details={"query_key": "x" * 501})


class TestIntegrationReconciliation(TestCase):
	def setUp(self) -> None:
		self.start = datetime(2026, 7, 28)
		self.end = datetime(2026, 7, 29)
		self.endpoint_row = Record(name="ENDPOINT-1", source_system="SOURCE-1")
		self.endpoint = Record(
			name="ENDPOINT-1",
			source_system="SOURCE-1",
			connector_key="oracle",
		)
		self.source = Record(name="SOURCE-1", enabled=1, read_only=1)
		self.counts = {
			"message_count": 5,
			"event_count": 5,
			"execution_count": 10,
			"error_count": 0,
		}

	def _run(
		self,
		snapshot=None,
		error: Exception | None = None,
		*,
		connector_key: str = "oracle",
		source: Record | None = None,
		log_side_effect=None,
	):
		self.endpoint.connector_key = connector_key
		connector = MagicMock()
		if error is not None:
			connector.reconcile.side_effect = error
		else:
			connector.reconcile.return_value = snapshot
		with (
			patch.object(integration, "assert_integration_endpoint_runtime"),
			patch.object(integration, "_ingestion_counts", return_value=self.counts),
			patch.object(integration.frappe, "get_doc", return_value=self.endpoint),
			patch.object(
				integration.frappe,
				"get_cached_doc",
				return_value=source or self.source,
			),
			patch.object(integration, "get_connector", return_value=connector) as get_connector,
			patch.object(
				integration,
				"_log_failure",
				side_effect=log_side_effect,
			) as log_failure,
			patch.object(integration, "materialize_data_reconciliation") as materialize,
			patch.object(integration, "_sync_reconciliation_issue"),
		):
			status = integration._reconcile_endpoint(self.endpoint_row, self.start, self.end)
		return status, materialize.call_args.args[0], log_failure, get_connector

	def test_unconfigured_source_query_is_pending_not_false_matched(self) -> None:
		status, values, _log, _get_connector = self._run(snapshot=None)
		self.assertEqual(status, "Pending")
		self.assertEqual(values["status"], "Pending")
		self.assertEqual(values["source_count"], 0)
		self.assertEqual(values["difference_count"], 0)
		details = json.loads(values["details_json"])
		self.assertFalse(details["source_truth_verified"])

	def test_blank_push_connector_is_pending_without_connector_lookup(self) -> None:
		status, values, _log, get_connector = self._run(connector_key="")
		self.assertEqual(status, "Pending")
		self.assertEqual(values["status"], "Pending")
		get_connector.assert_not_called()

	def test_verified_source_count_can_match_all_ingress_records(self) -> None:
		status, values, _log, _get_connector = self._run(
			ReconciliationSnapshot(
				5,
				source_hash="reviewed-hash",
				details={"query_key": "HOSPITAL_EVENT_COUNT"},
			)
		)
		self.assertEqual(status, "Matched")
		self.assertEqual(values["source_count"], 5)
		self.assertEqual(values["source_hash"], "reviewed-hash")
		self.assertEqual(values["difference_count"], 0)
		details = json.loads(values["details_json"])
		self.assertTrue(details["source_truth_verified"])
		self.assertEqual(details["source_query"]["query_key"], "HOSPITAL_EVENT_COUNT")

	def test_source_query_failure_is_explicit_and_payload_free(self) -> None:
		active_exceptions = []
		status, values, log, _get_connector = self._run(
			error=RuntimeError("secret source response"),
			log_side_effect=lambda *_args: active_exceptions.append(sys.exc_info()[0]),
		)
		self.assertEqual(status, "Failed")
		self.assertEqual(values["status"], "Failed")
		self.assertNotIn("secret source response", values["details_json"])
		log.assert_called_once()
		self.assertEqual(log.call_args.args[2], "RuntimeError")
		self.assertEqual(active_exceptions, [None])

	def test_disabled_source_is_failed_without_source_access(self) -> None:
		status, values, log, get_connector = self._run(
			source=Record(name="SOURCE-1", enabled=0, read_only=1),
		)
		self.assertEqual(status, "Failed")
		self.assertEqual(values["status"], "Failed")
		get_connector.assert_not_called()
		log.assert_called_once()

	def test_writable_source_is_failed_without_source_access(self) -> None:
		status, values, _log, get_connector = self._run(
			source=Record(name="SOURCE-1", enabled=1, read_only=0),
		)
		self.assertEqual(status, "Failed")
		self.assertEqual(values["status"], "Failed")
		get_connector.assert_not_called()

	def test_mismatch_opens_a_scoped_data_quality_issue(self) -> None:
		document = MagicMock()
		with (
			patch.object(integration.frappe.db, "get_value", return_value=None),
			patch.object(integration.frappe, "get_doc", return_value=document) as get_doc,
		):
			integration._sync_reconciliation_issue(
				endpoint=self.endpoint,
				reconciliation_name="RECON-1",
				period_start=self.start,
				period_end=self.end,
				status="Mismatch",
				counts=self.counts,
				source_count=7,
			)
		values = get_doc.call_args.args[0]
		self.assertEqual(values["status"], "Open")
		self.assertEqual(values["issue_type"], "Integration Reconciliation")
		self.assertEqual(values["expected_value"], "source_count=7")
		self.assertNotIn("patient", values["description"].lower())
		document.insert.assert_called_once_with(ignore_permissions=True)

	def test_verified_match_resolves_but_does_not_override_accepted_risk(self) -> None:
		document = MagicMock(status="Open")
		with (
			patch.object(integration.frappe.db, "get_value", return_value="DQI-1"),
			patch.object(integration.frappe.db, "advisory_lock", return_value=nullcontext()) as lock,
			patch.object(integration.frappe, "get_doc", return_value=document) as get_doc,
		):
			integration._sync_reconciliation_issue(
				endpoint=self.endpoint,
				reconciliation_name="RECON-1",
				period_start=self.start,
				period_end=self.end,
				status="Matched",
				counts=self.counts,
				source_count=5,
			)
		lock.assert_called_once_with("ione-qms:data-quality-review:DQI-1", timeout=5)
		get_doc.assert_called_once_with("IONE Data Quality Issue", "DQI-1", for_update=True)
		self.assertEqual(document.update.call_args.args[0]["status"], "Resolved")
		self.assertEqual(document.update.call_args.args[0]["resolved_by"], "Administrator")
		document.save.assert_called_once_with(ignore_permissions=True)

		accepted = MagicMock(status="Accepted")
		with (
			patch.object(integration.frappe.db, "get_value", return_value="DQI-1"),
			patch.object(integration.frappe.db, "advisory_lock", return_value=nullcontext()),
			patch.object(integration.frappe, "get_doc", return_value=accepted) as get_doc,
		):
			integration._sync_reconciliation_issue(
				endpoint=self.endpoint,
				reconciliation_name="RECON-1",
				period_start=self.start,
				period_end=self.end,
				status="Mismatch",
				counts=self.counts,
				source_count=7,
			)
		get_doc.assert_called_once_with("IONE Data Quality Issue", "DQI-1", for_update=True)
		accepted.update.assert_not_called()
		accepted.save.assert_not_called()

	def test_endpoint_failure_rolls_back_savepoint_and_continues_without_active_exception(self) -> None:
		endpoints = [
			Record(name="ENDPOINT-1", source_system="SOURCE-1"),
			Record(name="ENDPOINT-2", source_system="SOURCE-2"),
		]
		active_exceptions = []
		with (
			patch.object(integration, "_ensure_builtin_connectors"),
			patch.object(integration.frappe, "get_all", return_value=endpoints),
			patch.object(integration.frappe.db, "savepoint") as savepoint,
			patch.object(integration.frappe.db, "rollback") as rollback,
			patch.object(integration.frappe.db, "release_savepoint") as release,
			patch.object(
				integration.frappe.db,
				"advisory_lock",
				side_effect=[nullcontext(), nullcontext()],
			),
			patch.object(
				integration,
				"_reconcile_endpoint",
				side_effect=[RuntimeError("database write failed"), "Matched"],
			) as reconcile,
			patch.object(
				integration,
				"_log_failure",
				side_effect=lambda *_args: active_exceptions.append(sys.exc_info()[0]),
			) as log_failure,
		):
			summary = integration.reconcile_integrations(
				period_start="2026-07-28 00:00:00",
				period_end="2026-07-29 00:00:00",
			)
		self.assertEqual(
			summary,
			{"endpoints": 2, "matched": 1, "mismatch": 0, "pending": 0, "failed": 1},
		)
		self.assertEqual(reconcile.call_count, 2)
		self.assertEqual(savepoint.call_count, 2)
		rollback.assert_called_once_with(save_point="ione_reconciliation_endpoint_0")
		release.assert_called_once_with("ione_reconciliation_endpoint_1")
		log_failure.assert_called_once()
		self.assertEqual(log_failure.call_args.args[2], "RuntimeError")
		self.assertEqual(active_exceptions, [None])


class TestOracleReconciliationContract(TestCase):
	def setUp(self) -> None:
		self.endpoint = Record(
			name="ORACLE-ENDPOINT",
			modified="2026-07-29 00:00:00",
			source_system="SOURCE-1",
			timeout_seconds=30,
			username="reviewed-user",
			allowed_hosts='["oracle.internal"]',
			connector_options={
				"host": "oracle.internal",
				"service_name": "HIS",
				"reconciliation_query_key": "TEST_COUNT",
			},
		)
		self.builder = MagicMock(return_value=("SELECT COUNT(*) AS source_count FROM reviewed_view", {}))

	def _reconcile(self, row):
		with (
			patch.dict(
				builtin_connectors._ORACLE_RECONCILIATION_QUERIES,
				{"TEST_COUNT": self.builder},
			),
			patch.object(builtin_connectors, "_assert_oracle_circuit_closed"),
			patch.object(
				builtin_connectors,
				"_execute_oracle_reconciliation_query",
				return_value=row,
			),
			patch.object(builtin_connectors, "_record_oracle_failure") as failure,
			patch.object(builtin_connectors, "_record_oracle_success") as success,
			patch.object(builtin_connectors, "_record_slow_oracle_query"),
		):
			snapshot = builtin_connectors.OracleConnector(self.endpoint).reconcile(
				datetime(2026, 7, 28),
				datetime(2026, 7, 29),
			)
		return snapshot, failure, success

	def test_integral_decimal_is_accepted_before_circuit_success(self) -> None:
		snapshot, failure, success = self._reconcile({"source_count": Decimal("5"), "source_hash": "a" * 64})
		self.assertEqual(snapshot.source_count, 5)
		self.assertEqual(snapshot.source_hash, "a" * 64)
		failure.assert_not_called()
		success.assert_called_once_with(self.endpoint)

	def test_fractional_or_non_numeric_counts_fail_and_do_not_clear_circuit(self) -> None:
		for value in (Decimal("1.5"), 1.0, "1", True):
			with self.subTest(value=value):
				with self.assertRaises(builtin_connectors.frappe.ValidationError):
					self._reconcile({"source_count": value})

	def test_contract_failure_counts_toward_circuit_breaker(self) -> None:
		with (
			patch.dict(
				builtin_connectors._ORACLE_RECONCILIATION_QUERIES,
				{"TEST_COUNT": self.builder},
			),
			patch.object(builtin_connectors, "_assert_oracle_circuit_closed"),
			patch.object(
				builtin_connectors,
				"_execute_oracle_reconciliation_query",
				return_value={"source_count": Decimal("1.5")},
			),
			patch.object(builtin_connectors, "_record_oracle_failure") as failure,
			patch.object(builtin_connectors, "_record_oracle_success") as success,
		):
			with self.assertRaises(builtin_connectors.frappe.ValidationError):
				builtin_connectors.OracleConnector(self.endpoint).reconcile(
					datetime(2026, 7, 28),
					datetime(2026, 7, 29),
				)
		failure.assert_called_once()
		success.assert_not_called()

	def test_hash_over_64_characters_is_rejected_before_circuit_success(self) -> None:
		with (
			patch.dict(
				builtin_connectors._ORACLE_RECONCILIATION_QUERIES,
				{"TEST_COUNT": self.builder},
			),
			patch.object(builtin_connectors, "_assert_oracle_circuit_closed"),
			patch.object(
				builtin_connectors,
				"_execute_oracle_reconciliation_query",
				return_value={"source_count": 1, "source_hash": "x" * 65},
			),
			patch.object(builtin_connectors, "_record_oracle_failure") as failure,
			patch.object(builtin_connectors, "_record_oracle_success") as success,
		):
			with self.assertRaises(builtin_connectors.frappe.ValidationError):
				builtin_connectors.OracleConnector(self.endpoint).reconcile(
					datetime(2026, 7, 28),
					datetime(2026, 7, 29),
				)
		failure.assert_called_once()
		success.assert_not_called()

	def test_read_only_sql_rejects_for_update(self) -> None:
		with self.assertRaises(builtin_connectors.frappe.ValidationError):
			builtin_connectors._validate_read_only_sql("SELECT source_count FROM reviewed_view FOR UPDATE")
