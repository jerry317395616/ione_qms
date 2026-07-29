# Security policy

IONE QMS processes sensitive medical-quality data. Do not disclose suspected
vulnerabilities in public issues. Report them privately to the repository
owner and include the affected release, reproducible steps, and business
impact.

Secrets, patient-identifying data, and production exports must never be
committed. Every production release requires permission tests, dependency
review, backup verification, and an approved rollback plan.

AI agents run as dedicated service users with explicit tool allowlists. They
must not modify source clinical systems, approve workflows, close formal
findings, or receive unrestricted create/update/delete/execute tools.
