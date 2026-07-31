# Dependency vulnerability-exploitability statement

Reviewed: 2026-07-31. Re-review deadline: 2026-08-31, or immediately when the
official Flow/Frappe dependency constraints change.

This statement is narrow: it does not suppress unknown findings, new advisory
IDs, or a version drift. CI first requires the effective Bench to contain
`aiohttp==3.14.1`, `python-dotenv==1.2.2`, and the highest LiteLLM version
currently allowed by official Flow (`litellm==1.83.7`). It then permits only
the IDs in `pip-audit-vex.txt`.

## LiteLLM proxy advisories

The following advisories affect HTTP routes in the separately deployable
LiteLLM Proxy service, not the LiteLLM client functions used by IONE QMS and
Flow:

- `PYSEC-2026-388`: proxy Host-header authentication bypass.
- `PYSEC-2026-2601`: proxy custom-code guardrail sandbox escape.
- `PYSEC-2026-2598`: proxy API-key route privilege escalation.
- `PYSEC-2026-2600`: proxy `/user/update` privilege escalation.
- `PYSEC-2026-3479`: proxy MCP OAuth passthrough authentication bypass.
- `PYSEC-2026-3476`: privileged proxy connection-test local-file read.

IONE deploys no LiteLLM Proxy process or route. The application uses LiteLLM
only for provider/model identification; model traffic goes to an authenticated
private vLLM gateway. Flow agents and triggers remain disabled until their
clinical evaluation gate is approved. CI rejects imports of `litellm.proxy` in
the IONE and Flow application sources, and deployment acceptance verifies that
no LiteLLM proxy process is running.

## pdfkit advisory

`PYSEC-2026-2860` concerns JavaScript execution and local-file exfiltration
through `pdfkit.from_string`. The exact official Frappe releases used here
override pdfkit meta-option parsing and force both `disable-javascript` and
`disable-local-file-access` before calling `from_string`. CI verifies those
controls in the pinned Frappe source. IONE does not call pdfkit directly.

If any source contract, exact version, runtime process assertion, or network
control above fails, this VEX no longer applies and deployment must fail
closed. The exception must be removed when official compatible releases carry
the upstream fixes.
