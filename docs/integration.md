# Hospital integration contract

## Supported patterns

1. Signed push events through
   `/api/method/ione_qms.api.integration.receive_event`.
2. Reviewed incremental pull connectors registered in application code.
3. Approved HL7/FHIR or file adapters that normalize into the same event
   contract.
4. Virtual DocTypes only for low-risk, read-only, interactive views where
   latency and availability are acceptable. They are not used for auditable
   rule evidence.

## Event request

Headers for a signed endpoint:

```text
X-IONE-Endpoint: <endpoint document name>
X-IONE-Timestamp: <unix seconds>
X-IONE-Nonce: <16-128 character unique value>
X-IONE-Signature: hex(hmac_sha256(secret, timestamp + "." + nonce + "." + endpoint + "." + raw_body))
```

The push method is intentionally reachable as a Frappe Guest endpoint so a
source system does not need a second Frappe API credential. Guest access is
not anonymous access: it always requires the endpoint HMAC, even on a
non-production site and even if an endpoint was mistakenly configured as
unsigned. Named Frappe sessions do not weaken the production signature
requirement.

Body:

```json
{
  "event_type": "Encounter.Updated",
  "event_time": "2026-07-29T10:00:00+08:00",
  "source_record_type": "encounter",
  "source_record_id": "E-1001",
  "source_version": "7",
  "patient_reference": "P-1001",
  "encounter_reference": "E-1001",
  "department_reference": "CARD",
  "payload": {
    "status": "Discharged"
  }
}
```

The endpoint is the exact `X-IONE-Endpoint` value. The request must use
`application/json`; query/form event fields, duplicate JSON keys, and mismatched
body endpoint values are rejected. The MAC is calculated over the exact UTF-8
body bytes without reserialization.

Endpoint security is revalidated immediately before every inbound event, pull,
retry, and reconciliation source access—not only when the endpoint document is
saved. Switching the site into production therefore fails closed for an
already-enabled endpoint with disabled TLS verification, a missing inbound
CIDR allowlist, an unsigned ingress path, or any other invalid production
setting.

In production the application also rejects a multi-value
`X-Forwarded-For` chain, closing the common `proxy_add_x_forwarded_for`
leftmost-spoof path. The trusted Press edge must overwrite
`X-Forwarded-For` with the actual peer address rather than append
client-supplied values; the application check is a second fail-closed layer,
not a substitute for that edge configuration.

The idempotency key is SHA-256 over the immutable source namespace, authorized
tenant hospital, record type, record ID, and source version. It deliberately
does not contain the endpoint: the same source business record received
through a second endpoint cannot create another Event. A matching processed
message returns the first Message/Event as a duplicate; a matching in-flight
message remains bound to its first endpoint and is retried only through that
endpoint. Database uniqueness provides the final race-safe guarantee.

## HL7 v2 signed HTTP adapter

HL7 v2 ER7 enters through
`/api/method/ione_qms.api.integration.receive_hl7_v2`. This is an HTTP adapter,
not a raw TCP/MLLP listener. A hospital interface engine that originates MLLP
must terminate the TCP session, preserve the exact ER7 or MLLP-framed bytes,
and send those bytes in the HTTP body. Transport ACK/NAK ownership, retry
timing, and the HTTP-to-MLLP status mapping are part of that reviewed interface
engine contract.

The Endpoint must be enabled and use this exact configuration:

- Direction `Inbound`;
- Connector Type `HL7 v2` and Connector Key `hl7_v2`;
- Authentication Type `HMAC`, Require Signature enabled, a secret of at least
  32 characters, and the production source CIDR allowlist;
- `batch_size` as an integer from 1 through 500;
- `connector_options` containing exactly
  `{"source_timezone":"<IANA zone>","encoding":"utf-8"}` or the same object
  with `gb18030`.

The request uses the same endpoint, timestamp, nonce, and SHA-256 HMAC headers
as the JSON event endpoint. Its `Content-Type` must be
`application/hl7-v2+er7; charset=utf-8` (or `application/hl7-v2`) with exactly
one explicit charset matching the Endpoint encoding. Extra media parameters,
an absent/mismatched charset, and non-ER7 media types fail with HTTP 415.
The HMAC covers the exact request bytes, including any MLLP frame bytes; no
decode, newline conversion, or reserialization occurs before verification.

One request is limited to 2 MiB and 500 messages, with the Endpoint allowed to
set a lower message limit. The adapter accepts either a bounded unframed ER7
batch split only at `MSH` segments, or canonical MLLP frames
`0x0b + ER7 + 0x1c 0x0d`. It strictly decodes the configured charset, validates
the envelope, timestamp/timezone, message control ID, sequence/version,
segments, fields/components, and normalized-output bounds. The complete batch
is parsed and all limits are checked before the first Integration Message or
Clinical Quality Event is created. Frappe's request transaction supplies the
database rollback boundary for an unexpected request-level failure.

Normalization exposes stable paths such as
`payload.hl7.PID.field_3_component_1` and repeated segment names such as
`payload.hl7.OBX_2.field_5`. It does not infer a Patient, Encounter, Hospital,
Department, Ward, or clinical code from PID/PV1/OBX values. A hospital-approved
Mapping version performs those source-specific translations and is frozen on
the resulting Message/Event. MSH-10 is the source record ID and MSH-13 is the
source version (or `0` when absent), so reused identities with different
normalized bytes enter the normal conflict quarantine rather than overwriting
earlier evidence.

Production enablement requires captured synthetic/de-identified examples for
every accepted message type/version/charset/framing combination, negative
signature/replay/media/batch cases, duplicate and conflicting MSH-10 cases,
mapping and tenant-boundary evidence, gateway ACK/NAK/retry behavior, and
source-to-QMS reconciliation. The adapter remains disabled until the hospital
interface owner approves those contracts.

## Mapping

Every enabled Endpoint references one exact, published
`IONE Integration Mapping` version. The version contains a JSON object from
allowlisted QMS scope fields to either a safe dotted source path or a reviewed
constant:

```json
{
  "department": "payload.department",
  "hospital": {"constant": "Hospital A"},
  "medical_staff": "payload.attending_staff",
  "medical_group": "payload.care_team_code",
  "disease": "payload.primary_diagnosis_code",
  "surgery": "payload.procedure_code",
  "drg": "payload.drg_code",
  "dip": "payload.dip_code"
}
```

Only `patient_index`, `encounter_index`, `hospital`, `campus`, `department`,
`ward`, `responsible_staff`, the `medical_staff` compatibility source, and the
canonical `medical_group`, `disease`, `surgery`, `drg`, and `dip` indicator source
dimensions can be populated by mapping. Link values are resolved by approved
identity crosswalks; dimension values are bounded canonical codes, not
narrative or arbitrary Frappe document names.

The Mapping must belong to the same Source and Endpoint, be `Active`, have a
current effective period, and have a canonical combined snapshot whose SHA-256
matches its stored checksum. The snapshot binds both `mapping_json` (scope
resolution) and `master_data_json` (Patient/Encounter projection) to the same
version. An enabled Endpoint cannot use its legacy inline `mapping` field or
define `connector_options.master_data`, and the inline scope field cannot
coexist with `mapping_record`; there is never a second runtime mapping source
of truth. A disabled legacy Endpoint may retain either inline value only long
enough for a named Integration Administrator to create an approved Mapping
version, clear the legacy values, and complete negative tenant tests before
enablement.

Draft mappings may be edited. Once a version becomes Active, its code, source,
endpoint, version, scope JSON, master-data JSON, canonical snapshot, checksum,
version identity, and effective dates are immutable; any changed field
dictionary requires a new version. An Active version can only be retired after
every enabled Endpoint has switched away or been disabled, and a retired
version cannot be deleted or reactivated.

Every Integration Message and external Clinical Quality Event freezes the
Mapping record, code, version, checksum, and exact canonical snapshot. The
snapshot is a restricted, non-list field because reviewed constants can still
be operationally sensitive. Processing verifies the frozen checksum against
the immutable published record. Retries always apply the Message snapshot
under the current Endpoint/Source tenant policy, even after the Endpoint has
switched to a later Mapping version; a checksum, snapshot, or lineage mismatch
fails closed instead of silently remapping retained clinical data.

An endpoint that materializes Patient or Encounter indexes must use a reviewed
`master_data_json` on its governed Mapping version, including a
`source_version_strategy` of `integer` or `datetime`. The exact configuration is
part of the Message snapshot, so a retained retry cannot drift after either the
Endpoint or a later Mapping version changes. Each index stores the applied
source version, strategy, event time, and exact envelope hash. Updates take an
entity-level advisory lock and row lock; an older version or the same version
with different bytes is sent to dead-letter review and cannot overwrite the
current projection. Lexical version guessing is not permitted.

```json
{
  "enabled": 1,
  "entity": "Encounter",
  "source_version_strategy": "integer",
  "fields": {
    "source_encounter_id": "encounter_id",
    "patient_source_id": "patient_id",
    "medical_group": "care_team_code",
    "disease": "primary_diagnosis_code",
    "surgery": "procedure_code",
    "drg": "drg_code",
    "dip": "dip_code",
    "status": "status"
  }
}
```

## Incremental pull

Each endpoint stores a cursor and bounded batch size. The raw cursor is a
hidden, read-only, restricted operational field because FHIR next links and
source tokens can contain confidential capabilities. Integration Job audit
records store only SHA-256 fingerprints of the before/after cursor, never the
raw cursor. Jobs can be created and moved from `Running` to one terminal state
only by the application-owned identity capability; configuration roles and
`ignore_permissions` do not make the audit record editable. A connector:

- uses only the configured allowlisted host;
- uses read-only source credentials;
- applies deterministic ordering by modification timestamp plus primary key;
- returns a new cursor only after every row is either processed/idempotent or
  durably quarantined;
- sends every row through the same message/event idempotency path;
- records attempts, duration, counts, errors, and reconciliation watermark;
- backs off transient errors and moves terminal failures to manual review.

Schema-invalid and oversized rows are represented by a protected Integration
Message containing only a SHA-256 manifest and sanitized error category; their
raw clinical body is never retained. A scope violation is handled the same way
and source identifiers are hashed. Terminal stale/version-conflicting rows
remain protected for governed review. The job is `Partial`, records a separate
quarantined count, and later valid rows in the page continue. Only a retained,
hash-valid dead letter can be replayed; schema, size, or tenant-scope manifests
require a corrected new source event.

Connectors must check the raw source page count before transforming it. A REST
array or FHIR Bundle larger than the requested limit fails as one protocol
error and does not advance its cursor; connectors never truncate a source page.
Record-local failures are different: non-object REST items, malformed FHIR
entries/resources, invalid file rows, and invalid Oracle envelope rows become
bounded quarantine markers and do not discard later records in the same page.
Markers contain only a reason code, connector position, byte count, and
SHA-256 fingerprint—never the rejected clinical value or an exception message.
Only the canonical application marker type may retain this metadata: its
decoder enforces exact keys, connector/reason combinations, position schemas,
lowercase SHA-256 values, bounded integers, and a 2 KiB encoded limit. Any
ordinary string, subclass, extra field, noncanonical JSON, or unknown marker is
treated as untrusted input and replaced by a generic manifest.

FHIR watermarks are derived only from validated resource `meta.lastUpdated`
instants. An untrusted Bundle timestamp and the QMS receive clock never advance
source truth. If a final page has records but no safe monotonic watermark, the
connector returns the exact prior cursor. A partial page can therefore be
durably quarantined and safely polled again without guessing past unseen source
data; a full non-advancing page fails closed until its source paging contract is
corrected.

Every new FHIR search replays a source-time overlap. The default is 5 seconds;
`watermark_overlap_seconds` accepts only an integer from 1 through 300. With a
previous watermark `W`, the request uses `_lastUpdated=ge(W-overlap)`, while
the stored cursor remains the maximum of `W` and successfully accepted resource
`meta.lastUpdated` values. Boundary records are therefore deliberately replayed
through the normal idempotency path, including late resources with the same
timestamp, without moving the cursor backwards.

FHIR enablement requires the source to give `lastUpdated` a monotonic update
meaning and to return stable, same-search `next` links for deterministic
`_lastUpdated,_id` paging. The source clock, history/retention policy, and
inclusive `ge` search behavior must be acceptance-tested. The overlap reduces
clock-boundary and commit-delay gaps; it does not replace a stable paging
snapshot, correct source versioning, reconciliation, or idempotency.
Set the window from measured source publication latency; a record backdated
beyond the configured window can still be missed and must be detected by
reconciliation.

REST item-cursor mode follows the same rule when a partial malformed page has
no object carrying the reviewed cursor field: its records can be quarantined,
but the exact prior source cursor is returned. Production token-mode endpoints
must provide a reviewed page-level `next_cursor_path`.

Before enablement, REST page/offset integrations must prove deterministic
snapshot or keyset behavior under concurrent inserts and deletes. File Drop
sources must publish immutable files by atomic rename, use monotonically ordered
names, never put patient identifiers in file names, and never append to a
completed file. These are signed-off source contracts, not behaviors the
connector can safely infer.

File Drop reads physical lines with the same 2 MiB per-record limit as push
ingress. An oversized line is streamed into a hash and discarded from memory;
its marker retains only a SHA-256 of the file name, the one-based line number,
exact byte count, and content hash. JSONL requires one JSON object per physical
line. CSV requires a valid, unique single-line header and one record per
physical line; multiline CSV records are quarantined rather than interpreted
ambiguously.

Oracle CLOB/BLOB values are read in bounded chunks and counted by their actual
UTF-8/byte size. A value over the 2 MiB ingress limit becomes a record-local
quarantine marker; the LOB body is neither fully allocated nor copied into the
marker or error description.

Direct dynamic SQL stored in a DocType is prohibited. Oracle queries live in
code-reviewed connectors and must contain no write statements, anonymous
blocks, DDL, or multiple statements.

## Tenant binding

Every enabled Source System and Endpoint has an explicit hierarchical
`allowed_scopes` table. Each row starts with a Hospital and may narrow to
Campus, Department, and Ward. A hospital-only row means that hospital, never
all hospitals. Endpoint rows must be subsets of their Source System rows.
Empty policies are disabled/fail closed.

The source `identity_namespace` is generated once and cannot change. Patient,
Encounter, Integration Message, and Event identities are partitioned by the
authorized hospital and namespace. Patient/Encounter Link mappings are
resolved before an Integration Message is created; an index from another
hospital or namespace is rejected without retaining the submitted body.
Patient and Encounter source identity lookup uses an exact-case,
length-delimited `stable_source_key` SHA-256 over entity kind, Source System,
namespace, hospital, and source ID. This fixed 64-character value has a named
unique database constraint and is the cross-worker arbiter; the HMAC-derived
patient/encounter surrogate remains independently versioned and rotatable.
Legacy raw-tuple lookup uses binary comparisons only during the bounded
transition and never emits raw identifiers in a lock name or log.
Mapping, existing indexes, materialization, Message, and Event insertion all
pass the same central policy. Retry and replay use the current policy, so
tightening an allowlist cannot be bypassed by an older retained message.
Invalid source records retain only a bounded manifest. The sole exception is
the exact reviewed `ConnectorQuarantineRecord` marker, whose connector,
reason, position, content hash, and size schema is strictly decoded before the
sanitized marker is retained; arbitrary strings, dictionaries, subclasses,
extra keys, and malformed markers are never retained.

Only a named `IONE Integration Administrator` may create or change Source
Systems, Endpoints, Mappings, signing settings, or tenant policies.
`Administrator` is not a business configuration identity. `IONE Integration
Operator` is read/operations-only and requires explicit hospital User
Permissions to access scoped Messages and Data Quality Issues.

Legacy sources receive only a stable random namespace. No hospital, campus,
department, or ward is inferred. Existing Source Systems and Endpoints without
signed-off scope rows are disabled and remain disabled until an accountable
Integration Administrator configures and validates them.

## Reconciliation

For every source and time window, compare source count/hash summaries with
received messages, normalized events, processed executions, and failures.
Differences create `IONE Data Reconciliation` and `IONE Data Quality Issue`
records. Reconciliation does not silently repair or discard source evidence.

A connector reports source truth only through its reviewed `reconcile`
implementation. Oracle endpoints set `reconciliation_query_key` inside
`connector_options`; that key must be registered in application code with
`register_oracle_reconciliation_query`. The query receives bound
`period_start`/`period_end` values and must return exactly one row containing
a non-negative `source_count` and, optionally, a stable `source_hash`. SQL,
binds, credentials, row identifiers, and clinical values are never written to
the reconciliation record.

If no reviewed source query is configured, the record remains `Pending`; QMS
message/event equality is not misrepresented as source parity. Query failure
creates a sanitized `Failed` result, and a verified unequal aggregate creates
`Mismatch`. Hospital DBAs must supply and approve the read-only count
semantics for every production endpoint.

## Error contract

- 400: invalid payload or schema.
- 401: missing/invalid signature, stale timestamp, replay, or disallowed IP.
- 403: authenticated caller lacks endpoint access.
- 409: one source identity/version was reused with different bytes. The
  conflicting body is durably quarantined under a separate conflict identity;
  an exact retry of the original bytes remains an idempotent success.
- 413: payload above application limit.
- 5xx: transient application failure; caller may retry with the same source
  identity and nonce must be regenerated.

Error messages never echo secrets or full clinical payloads.

## Source-document deep links

`IONE Source Document Locator` is a governed, disabled-by-default mapping from
one source system, source record type, hospital scope, and optional narrower
scope to a static HTTPS path. A named Integration Administrator authors a
Draft; a different named QC Administrator or Medical Affairs user approves it.
Approved semantics and their endpoint/source contract checksum are immutable.
The configuration can only be suspended, resumed, or retired through POST
governance APIs.

Version 1 does not connect to Oracle, issue HTTP requests, follow redirects,
proxy document text, or store an arbitrary URL/template/query. It combines an
allowlisted HTTPS origin, a reviewed static path prefix, one percent-encoded
source record ID path segment, and an optional static suffix. Userinfo, query,
fragment, wildcard hosts, percent escapes in configuration, traversal, and
cross-host selectors are rejected. Each authorized attempt writes an immutable
`IONE Source Document Access Log` containing only the source-record hash and
governance lineage, never the raw ID or resolved URL.

No locator may be approved until the hospital supplies the real HTTPS host,
exact path format, existing browser SSO behavior, and UAT evidence showing that
the exact route does not redirect to another host. Without one matching
approved configuration, resolution remains fail closed.
