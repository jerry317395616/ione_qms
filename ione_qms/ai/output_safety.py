from __future__ import annotations

import re
import unicodedata

# Separator-tolerant matching is deliberately applied only to bounded identifier
# candidates and release-known values. Collapsing an entire clinical narrative
# would concatenate unrelated measurements and create widespread false positives.
_IDENTIFIER_SEPARATOR_CLASS = (
	r"[\s.\-"
	r"\u058a\u05be\u1400\u1806"
	r"\u2010-\u2015\u2e17\u2e1a\u2e3a-\u2e3b\u2e40"
	r"\u301c\u3030\u30a0\ufe31-\ufe32\ufe58\ufe63\uff0d"
	r"\u00b7\u0387\u2022\u2024\u2027\u2219\u22c5\u30fb\uff65"
	r"]"
)
_IDENTIFIER_SEPARATOR = re.compile(_IDENTIFIER_SEPARATOR_CLASS)
_SEPARATOR_TOLERANT_MOBILE = re.compile(
	r"(?<![0-9A-Za-z])"
	rf"(?:\+?{_IDENTIFIER_SEPARATOR_CLASS}*86{_IDENTIFIER_SEPARATOR_CLASS}*)?"
	rf"1{_IDENTIFIER_SEPARATOR_CLASS}*[3-9]"
	rf"(?:{_IDENTIFIER_SEPARATOR_CLASS}*\d){{9}}"
	r"(?![0-9A-Za-z])",
)
_SEPARATOR_TOLERANT_IDENTITY_NUMBER = re.compile(
	r"(?<![0-9A-Za-z])"
	rf"(?:\d{_IDENTIFIER_SEPARATOR_CLASS}*){{17}}[0-9Xx]"
	r"(?![0-9A-Za-z])",
)

_DIRECT_IDENTIFIER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
	(
		"DIRECT_MOBILE_NUMBER",
		re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
	),
	(
		"DIRECT_IDENTITY_NUMBER",
		re.compile(r"(?<![0-9A-Za-z])\d{17}[0-9Xx](?![0-9A-Za-z])"),
	),
	(
		"DIRECT_EMAIL_ADDRESS",
		re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
	),
	(
		"EXTERNAL_OR_DEEP_LINK",
		re.compile(r"\b(?:https?|ftp)://\S+", re.IGNORECASE),
	),
	(
		"LABELED_CLINICAL_IDENTIFIER",
		re.compile(
			r"(?:患者(?:姓名|编号|ID)?|病人(?:姓名|编号|ID)?|姓名|"
			r"身份证(?:号|号码)?|证件号码|住院号|门诊号|病案号|医保号|床号|"
			r"出生日期|生日|地址|住址|家庭住址|联系电话|手机(?:号|号码)?|"
			r"patient(?:[_\s-]?(?:id|name))?|"
			r"encounter(?:[_\s-]?id)?|medical[_\s-]?record(?:[_\s-]?id)?|mrn)"
			r"\s*[:\uff1a=]\s*(?!\[REDACTED\])[^\s,\uff0c;\uff1b|]{2,}",
			re.IGNORECASE,
		),
	),
	(
		"RAW_CLINICAL_RECORD_REFERENCE",
		re.compile(
			r"\bIONE\s+(?:Patient Index|Encounter Index|Medical Record QC|"
			r"QC Finding Evidence)\s*[:#]",
			re.IGNORECASE,
		),
	),
)


def ai_content_privacy_violation(
	content: str,
	*,
	known_identifiers: tuple[str, ...] = (),
) -> str | None:
	"""Return a content-free PHI/DLP reason code for model-controlled text."""
	normalized = unicodedata.normalize("NFKC", str(content or ""))
	for character in normalized:
		if character in "\n\r\t":
			continue
		if unicodedata.category(character) in {"Cc", "Cf"}:
			return "HIDDEN_OR_CONTROL_CHARACTER"
	folded = normalized.casefold()
	for identifier in known_identifiers:
		pattern = _known_identifier_pattern(identifier)
		if pattern is not None and pattern.search(folded):
			return "KNOWN_IDENTITY_VALUE"
	if _SEPARATOR_TOLERANT_MOBILE.search(normalized):
		return "DIRECT_MOBILE_NUMBER"
	if _SEPARATOR_TOLERANT_IDENTITY_NUMBER.search(normalized):
		return "DIRECT_IDENTITY_NUMBER"
	for code, pattern in _DIRECT_IDENTIFIER_PATTERNS:
		if pattern.search(normalized):
			return code
	return None


def _known_identifier_pattern(identifier: str) -> re.Pattern[str] | None:
	"""Build a bounded separator-tolerant matcher for one release-known value."""
	known = unicodedata.normalize("NFKC", str(identifier or "")).strip().casefold()
	canonical = "".join(character for character in known if not _IDENTIFIER_SEPARATOR.fullmatch(character))
	if len(canonical) < 2:
		return None
	body = rf"{_IDENTIFIER_SEPARATOR_CLASS}*".join(re.escape(character) for character in canonical)
	if canonical.isdigit():
		return re.compile(rf"(?<!\d){body}(?!\d)")
	if any(character.isascii() and character.isalnum() for character in canonical):
		return re.compile(rf"(?<![0-9a-z]){body}(?![0-9a-z])")
	return re.compile(body)


def aggregate_report_output_violation(content: str) -> str | None:
	"""Return a stable fail-closed reason code without returning sensitive text."""
	return ai_content_privacy_violation(content)
