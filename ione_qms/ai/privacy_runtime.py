from __future__ import annotations

from typing import Any

import frappe


def task_known_identifiers(task: Any) -> tuple[str, ...]:
	"""Load bounded direct identifiers for DLP matching without returning them."""
	patient = str(task.get("patient") or "")
	encounter = str(task.get("encounter") or "")
	values: set[str] = {value for value in (patient, encounter) if len(value.strip()) >= 2}
	if encounter:
		row = frappe.db.get_value(
			"IONE Encounter Index",
			encounter,
			["patient", "encounter_no", "source_encounter_id"],
			as_dict=True,
		)
		if row:
			patient = patient or str(row.get("patient") or "")
			values.update(
				str(row.get(fieldname) or "").strip() for fieldname in ("encounter_no", "source_encounter_id")
			)
	if patient:
		row = frappe.db.get_value(
			"IONE Patient Index",
			patient,
			["patient_name", "source_patient_id", "date_of_birth"],
			as_dict=True,
		)
		if row:
			values.update(
				str(row.get(fieldname) or "").strip()
				for fieldname in ("patient_name", "source_patient_id", "date_of_birth")
			)
	return tuple(sorted((value for value in values if len(value) >= 2), key=len, reverse=True))
