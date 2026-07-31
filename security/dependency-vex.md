# Dependency vulnerability-exploitability statement

Reviewed: 2026-07-31. Re-review deadline: 2026-08-31, or immediately when the
official Flow/Frappe dependency constraints change.

This statement is narrow: it does not suppress unknown findings, new advisory
IDs, or a version drift. CI first requires the effective Bench to contain the
only resolver-tested LiteLLM version at the intersection of official Flow and
Frappe (`litellm==1.83.0`) and the two exact transitive versions required by that
release (`aiohttp==3.13.5` and `python-dotenv==1.0.1`). It then permits only the
IDs in `pip-audit-vex.txt`.

## LiteLLM proxy advisories

The following advisories affect HTTP routes in the separately deployable
LiteLLM Proxy service, not the LiteLLM client functions used by IONE QMS and
Flow:

- `PYSEC-2026-388`: proxy Host-header authentication bypass.
- `PYSEC-2026-391`: proxy API-key verification SQL injection.
- `PYSEC-2026-2602`: proxy `/prompts/test` template injection.
- `PYSEC-2026-2601`: proxy custom-code guardrail sandbox escape.
- `PYSEC-2026-2599`: proxy MCP stdio test command execution.
- `PYSEC-2026-2598`: proxy API-key route privilege escalation.
- `PYSEC-2026-2600`: proxy `/user/update` privilege escalation.
- `PYSEC-2026-3477`: proxy Skills archive path traversal.
- `PYSEC-2026-3479`: proxy MCP OAuth passthrough authentication bypass.
- `PYSEC-2026-3476`: privileged proxy connection-test local-file read.

IONE deploys no LiteLLM Proxy process or route. The application uses LiteLLM
only for provider/model identification; model traffic goes to an authenticated
private vLLM gateway. Flow agents and triggers remain disabled until their
clinical evaluation gate is approved. CI rejects imports of `litellm.proxy` in
the IONE and Flow application sources, and deployment acceptance verifies that
no LiteLLM proxy process is running.

## LiteLLM-pinned aiohttp and python-dotenv advisories

LiteLLM 1.83.0 has exact metadata pins for aiohttp 3.13.5 and python-dotenv
1.0.1, so these transitive packages cannot be upgraded without violating the
official Flow/LiteLLM resolver contract.

The covered aiohttp IDs are `PYSEC-2026-237`, `PYSEC-2026-2104`,
`PYSEC-2026-2105`, `PYSEC-2026-2106`, `PYSEC-2026-2107`,
`PYSEC-2026-2108`, `PYSEC-2026-2109`, `PYSEC-2026-2110`,
`PYSEC-2026-2111`, `PYSEC-2026-2112`, and `PYSEC-2026-2113`. They affect
aiohttp server parsing/WebSocket/resource paths, cookie persistence,
per-request cookies or authentication across redirects, multipart construction,
or per-request TLS SNI overrides. IONE, Flow, Drive, and Frappe do not import
aiohttp directly. IONE invokes only the LiteLLM client path against one fixed,
credential-free, private HTTP base URL; the authenticated Qwen gateway does
not redirect and the call carries neither cookies, multipart bodies, nor a
per-request TLS override. No aiohttp server is started.

`PYSEC-2026-2270` affects python-dotenv `set_key()`/`unset_key()` following a
locally planted symlink during a cross-device fallback. The application
sources do not import those functions, and the managed Bench has no untrusted
local shell user. The package is present only because LiteLLM pins it.

CI rejects application-source imports of aiohttp, `litellm.proxy`, and the
vulnerable python-dotenv mutation functions. If a future feature needs any of
those surfaces, the build fails until the dependency is upgraded or the VEX is
re-reviewed.

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
