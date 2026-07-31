from __future__ import annotations

from unittest import TestCase

from ione_qms.ai.output_safety import (
	aggregate_report_output_violation,
	ai_content_privacy_violation,
)
from ione_qms.ai.privacy import (
	AIPrivacyViolation,
	assert_model_output_privacy,
	deidentified_content_hash,
	deidentify_ai_value,
)


class TestAggregateReportOutputSafety(TestCase):
	def test_allows_aggregate_quality_language(self) -> None:
		self.assertIsNone(
			aggregate_report_output_violation("本月共完成 120 例聚合统计, 达标率 96.5%, 未包含个体身份信息。")
		)

	def test_rejects_direct_identifiers_and_links_with_stable_codes(self) -> None:
		cases = {
			"联系电话\uff1a13800138000": "DIRECT_MOBILE_NUMBER",
			"身份证号\uff1a11010519491231002X": "DIRECT_IDENTITY_NUMBER",
			"contact@example.org": "DIRECT_EMAIL_ADDRESS",
			"https://emr.example.org/document/1": "EXTERNAL_OR_DEEP_LINK",
			"患者姓名\uff1a张三": "LABELED_CLINICAL_IDENTIFIER",
			"出生日期\uff1a1980-01-01": "LABELED_CLINICAL_IDENTIFIER",
			"家庭住址\uff1a某市某路 8 号": "LABELED_CLINICAL_IDENTIFIER",
			"MRN: A-000123": "LABELED_CLINICAL_IDENTIFIER",
			"IONE Patient Index:PAT-0001": "RAW_CLINICAL_RECORD_REFERENCE",
		}
		for content, expected in cases.items():
			with self.subTest(content=content):
				self.assertEqual(aggregate_report_output_violation(content), expected)

	def test_rejects_hidden_format_characters(self) -> None:
		self.assertEqual(
			aggregate_report_output_violation("聚合报\u200b告"),
			"HIDDEN_OR_CONTROL_CHARACTER",
		)

	def test_known_identity_values_are_rejected_on_every_model_output_surface(self) -> None:
		self.assertEqual(
			ai_content_privacy_violation(
				"建议复核患者张三的记录",
				known_identifiers=("张三", "MRN-000123"),
			),
			"KNOWN_IDENTITY_VALUE",
		)

	def test_separator_obfuscated_identifiers_fail_closed(self) -> None:
		cases = {
			"contact 138-0013-8000": "DIRECT_MOBILE_NUMBER",
			"contact 138\u00b70013\uff0e8000": "DIRECT_MOBILE_NUMBER",
			"ID 110105-19491231-002X": "DIRECT_IDENTITY_NUMBER",
			"ID 110105\u201319491231\u2022002X": "DIRECT_IDENTITY_NUMBER",
		}
		for content, expected in cases.items():
			with self.subTest(content=content):
				self.assertEqual(ai_content_privacy_violation(content), expected)
		self.assertEqual(
			ai_content_privacy_violation(
				"patient Z hangSan",
				known_identifiers=("ZhangSan",),
			),
			"KNOWN_IDENTITY_VALUE",
		)
		self.assertEqual(
			ai_content_privacy_violation(
				"patient Z\u00b7hang.San",
				known_identifiers=("ZhangSan",),
			),
			"KNOWN_IDENTITY_VALUE",
		)

	def test_separator_tolerance_preserves_identifier_boundaries(self) -> None:
		self.assertIsNone(ai_content_privacy_violation("I10 - E11.9 - J18.9"))
		self.assertIsNone(ai_content_privacy_violation("contact 138-0013-80001"))
		self.assertIsNone(
			ai_content_privacy_violation(
				"preZ hangSanPost",
				known_identifiers=("ZhangSan",),
			)
		)
		self.assertIsNone(
			ai_content_privacy_violation(
				"9123-45-6",
				known_identifiers=("12345",),
			)
		)

	def test_nested_ai_input_is_deidentified_and_manifest_contains_no_plaintext(self) -> None:
		raw = {
			"patient_name": "张三",
			"diagnoses": [
				{
					"code": "I10",
					"note": "患者姓名\uff1a张三\uff0c诊断已确认",
					"source_patient_id": "MRN-000123",
				}
			],
		}
		safe = deidentify_ai_value(
			raw,
			known_identifiers=("张三", "MRN-000123"),
		)
		self.assertNotIn("patient_name", safe)
		self.assertNotIn("source_patient_id", safe["diagnoses"][0])
		self.assertEqual(safe["diagnoses"][0]["note"], "患者姓名:[REDACTED],诊断已确认")
		manifest_hash = deidentified_content_hash(safe)
		self.assertEqual(len(manifest_hash), 64)
		self.assertNotIn("张三", manifest_hash)

	def test_unknown_direct_identifiers_fail_closed_before_qwen(self) -> None:
		for raw in (
			{"note": "联系电话\uff1a13800138000"},
			{"care_timeline": "contact 138-0013-8000"},
			{"nested": [{"value": "11010519491231002X"}]},
			{"care_timeline": "ID 110105-19491231-002X"},
			{"note": "patient@example.org"},
		):
			with self.subTest(raw=raw), self.assertRaises(AIPrivacyViolation):
				deidentify_ai_value(raw, known_identifiers=())

	def test_known_separator_obfuscation_fails_on_input_and_output_contracts(self) -> None:
		with self.assertRaises(AIPrivacyViolation):
			deidentify_ai_value(
				{"care_timeline": "patient Z hangSan"},
				known_identifiers=("ZhangSan",),
			)
		with self.assertRaises(AIPrivacyViolation):
			assert_model_output_privacy(
				"patient Z hangSan",
				known_identifiers=("ZhangSan",),
			)
		self.assertEqual(
			aggregate_report_output_violation("contact 138-0013-8000"),
			"DIRECT_MOBILE_NUMBER",
		)
