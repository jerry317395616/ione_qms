from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import frappe

from ione_qms.ai.tool_registry import (
	IONE_FLOW_TOOL_BY_SLUG,
	assert_ione_tool_integrity,
	live_tool_schema_manifest,
	tool_registry_fingerprint,
)

COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
RUNTIME_DEPENDENCY_APPS = ("frappe", "flow", "ione_core", "drive")
REQUIRED_RUNTIME_APPS = frozenset({"frappe", "flow"})


def build_release_configuration(policy, agent=None, model=None) -> dict[str, Any]:
	"""Return the secret-free, canonical configuration reviewed by an Agent release."""
	agent = agent or frappe.get_doc("Flow Agent", policy.flow_agent)
	model = model or frappe.get_doc("Flow Model", agent.model)
	tool_slugs = sorted({str(row.tool) for row in agent.get("tools") or []})
	assert_ione_tool_integrity(set(tool_slugs))
	model_params = _json_mapping(model.get("params"), "Flow Model params")
	provider_configuration = flow_runtime_provider_configuration(model)
	active_provider = (
		provider_configuration
		if provider_configuration and provider_configuration["exists"] and provider_configuration["enabled"]
		else None
	)
	provider_params = active_provider["extra_params"] if active_provider else {}
	effective_params = {**provider_params, **model_params}
	knowledge_manifest = build_knowledge_base_manifest(agent)
	tool_schemas = live_tool_schema_manifest(set(tool_slugs))
	return {
		"contract_version": 3,
		"runtime_commits": runtime_commit_manifest(),
		"policy": {
			"name": policy.name,
			"policy_code": policy.get("policy_code"),
			"agent_category": policy.get("agent_category"),
			"flow_agent": policy.get("flow_agent"),
			"service_user": policy.get("service_user"),
			"allowed_tools": sorted({str(row.tool) for row in policy.get("allowed_tools") or []}),
			"allowed_hospitals": sorted(
				{
					str(row.get("hospital"))
					for row in policy.get("allowed_hospitals") or []
					if row.get("hospital")
				}
			),
			"allowed_departments": sorted(
				{
					str(row.get("department"))
					for row in policy.get("allowed_departments") or []
					if row.get("department")
				}
			),
			"contains_patient_data": int(policy.get("contains_patient_data") or 0),
			"requires_human_review": int(policy.get("requires_human_review") or 0),
			"maximum_records_per_run": int(policy.get("maximum_records_per_run") or 0),
			"allow_auto_approve": int(policy.get("allow_auto_approve") or 0),
			"allow_scheduled_run": int(policy.get("allow_scheduled_run") or 0),
		},
		"agent": {
			"name": agent.name,
			"title": agent.get("title"),
			"model": agent.get("model"),
			"enabled": int(agent.get("enabled") or 0),
			"max_iterations": int(agent.get("max_iterations") or 0),
			"instructions": agent.get("instructions") or "",
			"tools": tool_slugs,
		},
		"model": {
			"name": model.name,
			"model_id": model.get("model_id"),
			"linked_provider": model.get("provider"),
			"runtime_provider": (provider_configuration["name"] if provider_configuration else None),
			"base_url": model.get("base_url") or (active_provider["base_url"] if active_provider else None),
			"enabled": int(model.get("enabled") or 0),
			"context_window": int(model.get("context_window") or 0),
			"params": model_params,
			"effective_params": effective_params,
		},
		"provider": provider_configuration,
		"tools": [
			IONE_FLOW_TOOL_BY_SLUG[slug].document_values
			for slug in tool_slugs
			if slug in IONE_FLOW_TOOL_BY_SLUG
		],
		"tool_registry_fingerprint": tool_registry_fingerprint(set(tool_slugs)),
		"tool_schemas": tool_schemas,
		"tool_schema_fingerprint": hashlib.sha256(canonical_json(tool_schemas).encode()).hexdigest(),
		"knowledge_base_manifest": knowledge_manifest,
		"knowledge_base_manifest_hash": hashlib.sha256(
			canonical_json(knowledge_manifest).encode()
		).hexdigest(),
	}


def build_knowledge_base_manifest(agent) -> dict[str, Any]:
	"""Bind releases/evaluations to exact Flow KB sources without retaining source content."""
	knowledge_names = sorted(
		{
			str(row.get("knowledge_base") or "")
			for row in agent.get("knowledge_bases") or []
			if row.get("knowledge_base")
		}
	)
	settings_manifest = None
	if knowledge_names:
		settings = frappe.get_cached_doc("Flow Knowledge Settings")
		embedding_model_name = str(settings.get("embedding_model") or "")
		if not embedding_model_name:
			frappe.throw("Agent knowledge bases require a configured Flow embedding model")
		embedding_model = frappe.get_doc("Flow Model", embedding_model_name)
		if not int(embedding_model.get("enabled") or 0):
			frappe.throw("Agent knowledge base embedding model is disabled")
		settings_manifest = {
			"embedding_model": {
				"name": embedding_model.name,
				"model_id": embedding_model.get("model_id"),
				"provider": embedding_model.get("provider"),
				"base_url": embedding_model.get("base_url"),
				"params": _json_mapping(
					embedding_model.get("params"),
					"Flow embedding model params",
				),
				"runtime_provider": flow_runtime_provider_configuration(embedding_model),
				"enabled": int(embedding_model.get("enabled") or 0),
				"modified": embedding_model.get("modified"),
			},
			"embedding_dimension": int(settings.get("embedding_dimension") or 0),
			"search_type": settings.get("search_type"),
			"chunk_size": int(settings.get("chunk_size") or 0),
			"chunk_overlap": int(settings.get("chunk_overlap") or 0),
			"modified": settings.get("modified"),
		}
	manifest: list[dict[str, Any]] = []
	for name in knowledge_names:
		if not frappe.db.exists("Flow Knowledge Base", name):
			frappe.throw(f"Agent knowledge base no longer exists: {name}")
		knowledge_base = frappe.get_doc("Flow Knowledge Base", name)
		sources = frappe.get_all(
			"Flow Knowledge Source",
			filters={"knowledge_base": name},
			fields=[
				"name",
				"title",
				"source_type",
				"content",
				"file",
				"url",
				"reference_doctype",
				"filters",
				"content_fields",
				"auto_sync",
				"chunk_size",
				"chunk_overlap",
				"status",
				"chunk_count",
				"last_synced_at",
				"modified",
			],
			order_by="name asc",
			limit_page_length=1_000,
		)
		if len(sources) >= 1_000:
			frappe.throw("Agent knowledge base source manifest exceeds the governed limit")
		source_manifest = []
		for source in sources:
			file_hash = None
			if source.get("file"):
				file_hash = frappe.db.get_value("File", source.get("file"), "content_hash")
			source_manifest.append(
				{
					"name": source.get("name"),
					"title_hash": _hash_optional(source.get("title")),
					"source_type": source.get("source_type"),
					"source_payload_hash": _hash_optional(
						{
							"content": source.get("content"),
							"file": source.get("file"),
							"file_content_hash": file_hash,
							"url": source.get("url"),
							"reference_doctype": source.get("reference_doctype"),
							"filters": _json_value(source.get("filters")),
							"content_fields": source.get("content_fields"),
						}
					),
					"auto_sync": int(source.get("auto_sync") or 0),
					"chunk_size": int(source.get("chunk_size") or 0),
					"chunk_overlap": int(source.get("chunk_overlap") or 0),
					"status": source.get("status"),
					"chunk_count": int(source.get("chunk_count") or 0),
					"last_synced_at": source.get("last_synced_at"),
					"modified": source.get("modified"),
				}
			)
		manifest.append(
			{
				"name": knowledge_base.name,
				"enabled": int(knowledge_base.get("enabled") or 0),
				"description_hash": _hash_optional(knowledge_base.get("description")),
				"modified": knowledge_base.get("modified"),
				"sources": source_manifest,
			}
		)
	return {
		"settings": settings_manifest,
		"knowledge_bases": manifest,
	}


def flow_runtime_provider_configuration(model) -> dict[str, Any] | None:
	"""Snapshot the non-secret Flow Provider inputs used by ``flow.lib.model.Model``."""
	provider_name = _runtime_provider_name(str(model.get("model_id") or ""))
	if not provider_name:
		return None
	if not frappe.db.exists("Flow Provider", provider_name):
		return {
			"name": provider_name,
			"exists": 0,
			"enabled": 0,
			"base_url": None,
			"extra_params": {},
		}
	provider = frappe.get_doc("Flow Provider", provider_name)
	return {
		"name": provider_name,
		"exists": 1,
		"enabled": int(provider.get("enabled") or 0),
		"base_url": provider.get("base_url") or None,
		"extra_params": _json_mapping(provider.get("extra_params"), "Flow Provider extra_params"),
	}


def _runtime_provider_name(model_id: str) -> str | None:
	"""Resolve the same provider key that Flow asks LiteLLM to resolve."""
	try:
		import litellm

		provider = litellm.get_llm_provider(model_id)[1]
	except Exception:
		# Reviewed IONE models are stored in canonical provider/model form.
		# This deterministic fallback also keeps release inspection available
		# during maintenance when LiteLLM cannot initialize.
		provider = model_id.partition("/")[0] if "/" in model_id else ""
	return str(provider or "").strip().lower() or None


def assert_approved_agent_release(policy, agent=None, model=None):
	"""Bind every model call to one immutable, current-code Approved release."""
	agent = agent or frappe.get_doc("Flow Agent", policy.flow_agent)
	model = model or frappe.get_doc("Flow Model", agent.model)
	name = frappe.db.get_value(
		"IONE Agent Release",
		{
			"policy": policy.name,
			"flow_agent": agent.name,
			"flow_model": model.name,
			"status": "Approved",
		},
		"name",
		order_by="approved_at desc",
	)
	if not name:
		frappe.throw("An Approved IONE Agent Release is required before this policy can run")
	release = frappe.get_doc("IONE Agent Release", name)
	expected = canonical_json(build_release_configuration(policy, agent, model))
	checksum = hashlib.sha256(expected.encode()).hexdigest()
	if release.get("configuration_json") != expected or release.get("checksum") != checksum:
		frappe.throw("Approved IONE Agent Release configuration checksum no longer matches runtime")
	current_sha = current_app_commit_sha()
	if str(release.get("commit_sha") or "").lower() != current_sha:
		frappe.throw("Approved IONE Agent Release does not match the running IONE QMS commit")
	return release


def current_app_commit_sha() -> str:
	configured = str(frappe.conf.get("ione_qms_commit_sha") or "").strip().lower()
	if configured:
		if not COMMIT_SHA_PATTERN.fullmatch(configured):
			frappe.throw("site_config ione_qms_commit_sha must be a full 40-character Git SHA")
	actual = _actual_app_commit_sha("ione_qms")
	if configured and configured != actual:
		frappe.throw("site_config ione_qms_commit_sha does not match the running IONE QMS Git HEAD")
	return actual


def runtime_commit_manifest() -> dict[str, str]:
	"""Return exact clean Git HEADs for every AI-runtime-affecting installed app."""
	try:
		installed = {str(app) for app in frappe.get_installed_apps()}
	except Exception as exc:
		raise frappe.ValidationError(
			"Cannot determine installed apps for the governed AI release manifest"
		) from exc
	commits = {"ione_qms": current_app_commit_sha()}
	for app_name in RUNTIME_DEPENDENCY_APPS:
		if app_name in REQUIRED_RUNTIME_APPS or app_name in installed:
			commits[app_name] = _actual_app_commit_sha(app_name)
	return dict(sorted(commits.items()))


def _actual_app_commit_sha(app_name: str) -> str:
	try:
		from git import Repo

		repo = Repo(frappe.get_app_source_path(app_name))
		sha = str(repo.head.commit.hexsha).lower()
		if repo.is_dirty(untracked_files=True):
			frappe.throw(f"The running {app_name} checkout is dirty; governed AI is disabled")
	except Exception as exc:
		if isinstance(exc, frappe.ValidationError):
			raise
		raise frappe.ValidationError(
			f"Cannot determine the running {app_name} Git commit; governed AI is disabled"
		) from exc
	if not COMMIT_SHA_PATTERN.fullmatch(sha):
		frappe.throw(f"The running {app_name} checkout did not provide a full Git commit SHA")
	return sha


def canonical_json(value: Any) -> str:
	return json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)


def _json_value(value: Any) -> Any:
	if value in (None, ""):
		return None
	if isinstance(value, str):
		try:
			return json.loads(value)
		except TypeError, ValueError:
			return value
	return value


def _json_mapping(value: Any, label: str) -> dict[str, Any]:
	parsed = _json_value(value)
	if parsed is None:
		return {}
	if not isinstance(parsed, dict):
		frappe.throw(f"{label} must be a JSON object")
	return parsed


def _hash_optional(value: Any) -> str | None:
	if value in (None, "", {}):
		return None
	return hashlib.sha256(canonical_json(value).encode()).hexdigest()
