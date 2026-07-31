# IONE QMS V1.0 需求追踪矩阵

## 1. 审计基线

- 审计日期：2026-07-29。
- 需求来源：`IONE医院医疗质量管理系统总体技术建设方案_V1.0.docx`，共 43 页。
- 代码范围：单一自研应用 `ione_qms`；Frappe 与 Frappe Flow 官方应用不在本仓库修改范围。
- 当前静态清单：124 个 DocType、9 个 Module、9 个 Workspace、8 个 Workflow、5 个 Flow Agent 蓝图。
- 当前交付状态：尚无同时包含远端候选 SHA、成功 Bench CI、Press QA
  和 `manager` 验收的发布证据包。
- 本矩阵只证明“仓库中有什么”；代码存在不等于临床口径已批准，也不等于生产验收通过。

## 2. 状态定义

| 状态 | 含义 |
| --- | --- |
| `Implemented` | 仓库中已有可定位代码、元数据或配置；不表示已经运行验收。 |
| `Static-verified` | 已通过元数据校验、语法检查或不依赖真实 Frappe 站点的自动化测试。 |
| `Runtime-unverified` | 必须在真实 Bench、Frappe v17、Flow develop、Qwen、Press 或 `manager` 站点验证。 |
| `External/Clinical blocker` | 依赖医院接口、临床口径、金样本、审批、基础设施或授权，不能由代码替代。 |
| `Not implemented` | 方案要求的制品或可审计证据目前不存在。 |

同一能力可同时为 `Implemented`、`Static-verified` 和 `Runtime-unverified`；这表示代码与静态验证已完成，但运行验收仍未完成。

## 3. 方案章节追踪

| ID | 方案章节 | 能力与当前实现 | 代码/对象证据 | 验证状态 | 外部或验收门槛 |
| --- | --- | --- | --- | --- | --- |
| S00 | 第 0 节 执行摘要 | 单 App 九模块、确定性规则优先、AI 仅提供受控建议 | `modules.txt`、`rule_engine/`、`services/ai_policy.py` | `Implemented`; `Static-verified`; `Runtime-unverified` | 需核验官方 App 工作树、构建 SHA 和站点开关 |
| S01 | 第 1 节 建设目标 | 标准、规则、指标、质控、整改、分析、集成、AI 治理底座已编码 | 124 个 DocType；9 个模块目录 | `Implemented`; `Static-verified`; `Runtime-unverified` | 真实临床专题与医院数据仍阻塞 |
| S02 | 第 2 节 政策与十项目标 | 提供指标/规则版本模型及初始化模板，不代表十项目标已落地 | `setup/blueprint.py`、规则与指标版本 DocType | `Implemented`; `Runtime-unverified` | `External/Clinical blocker`：医院口径、责任科室、基线、目标值、审批 |
| S03 | 第 3 节 设计原则 | 模块化单体、服务层、只读源系统、AI 最小权限已体现 | `services/`、`integration/`、`ai/` | `Static-verified`; `Runtime-unverified` | 需真实权限负向测试和审计取证 |
| S04 | 第 4 节 技术基线 | 按 Frappe v17 develop 与 Flow develop 元数据设计 | `pyproject.toml`、`hooks.py`、`setup/install.py` | `Static-verified`; `Runtime-unverified` | 必须固定并记录三个仓库的已测 commit SHA |
| S05 | 第 5 节 总体架构 | 接入、领域、规则、指标、AI、展示分层均有代码骨架 | `integration/`、`rule_engine/`、`indicator_engine/`、`api/` | `Implemented`; `Static-verified` | 真实端到端链路尚未在站点运行 |
| S06 | 第 6 节 九模块设计 | 九模块及九 Workspace 已创建 | `modules.txt`、各模块 `workspace/*.json` | `Implemented`; `Static-verified`; `Runtime-unverified` | Desk 路由、角色可见性及工作台查询需 Bench 验证 |
| S07 | 第 7 节 领域模型 | 主数据、版本、问题、整改、申诉、集成、AI、审计对象共 124 个 DocType | 各模块 `doctype/**.json` | `Implemented`; `Static-verified`; `Runtime-unverified` | 需 `bench migrate`、建表、索引和权限实测 |
| S08 | 第 8 节 业务功能 | 标准/规则/运行与终末质控/手麻/事件/整改/角色工作台均有通用实现 | `services/findings.py`、`services/improvement.py`、`services/safety_events.py`、`api/dashboard.py` | `Implemented`; `Static-verified`; `Runtime-unverified` | 专科规则包、真实事件生产链和用户验收未完成 |
| S09 | 第 9 节 规则引擎 | 确定性执行、证据、去重、动作约束、回放与生产安全开关已编码 | `rule_engine/`、`api/rules.py`、`tests/test_rule_*.py` | `Static-verified`; `Runtime-unverified` | 实时执行默认关闭；需 30–50 条临床批准规则、金样本与回放报告 |
| S10 | 第 10 节 指标引擎 | 指标元数据、版本、趋势、事实与计算器注册机制已编码 | `indicator_engine/`、`services/indicators.py`、`api/indicator.py` | `Static-verified`; `Runtime-unverified` | 分子/分母、排除条件、数据源和历史基线须医院确认 |
| S11 | 第 11 节 集成与治理 | 来源、端点、映射、消息、任务、数据质量、对账、来源对账及严格签名的 HL7 v2 HTTP 适配器已编码 | `integration/`、`integration/hl7_v2.py`、`api/integration.py`、`tasks/integration.py`、`tests/test_hl7_v2_static.py` | `Static-verified`; `Runtime-unverified` | `External/Clinical blocker`：源合同、字段字典、只读账号、实际 Oracle 对账 query，以及医院接口引擎的 HL7 消息类型/字符集/MLLP ACK 与重试合同 |
| S12 | 第 12 节 Flow AI | 5 个 Agent、最小工具、初始/Resume claim/lease/CAS、审批消费、孤儿运行对账、不可变 Flow 修订、评测和运行开关已编码 | `setup/install.py`、`ai/`、`services/agent_evaluations.py` | `Static-verified`; `Runtime-unverified` | AI 默认关闭；Qwen 鉴权、Flow Trigger、崩溃恢复、并发和人工续跑未验收 |
| S13 | 第 13 节 权限安全 | 角色、临床范围、受控导出、安全策略、安全事件上报和审计钩子已编码；导出请求从规范化过滤器固化医院/院区/科室范围，Auditor 仅可处理明确 User Permission 覆盖的单科室/单院区请求，全院/多范围/无范围请求仅 Medical Affairs；审批、驳回、生成共享并发锁，私有文件下载复核父请求范围和有效期 | `permissions.py`、`services/data_export.py`、`services/safety_events.py`、`tasks/security.py` | `Static-verified`; `Runtime-unverified` | 需在 Bench 以真实 User Permission 执行 Desk/API/私有文件下载和审批竞态负向测试 |
| S14 | 第 14 节 性能高可用 | 队列与降级原则有文档和开关，尚无容量证据 | `hooks.py`、运行设置与任务配置 | `Implemented`; `Runtime-unverified` | `Not implemented`：压力报告、容量模型、HA 故障切换与可用性证据 |
| S15 | 第 15 节 部署环境 | 有安装、发布、回滚和运维说明 | `docs/operations.md`、`docs/release-and-rollback.md` | `Runtime-unverified` | `Not implemented`：Press QA 发布记录与 `manager` 安装验收 |
| S16 | 第 16 节 工程与 Hook | Controller/Service 分层、文档事件和定时任务钩子已编码 | `hooks.py`、`services/`、各 DocType controller | `Static-verified`; `Runtime-unverified` | scheduler 实际启用、队列消费及失败重试未验证 |
| S17 | 第 17 节 分支与 CI/CD | 应用使用 develop；有校验工具与双 Frappe 基线 CI | `tools/validate_app.py`、`.github/workflows/` | `Static-verified`; `Runtime-unverified` | 尚未取得与最终候选 SHA 绑定的成功远端 CI 和不可变 Press 制品 |
| S18 | 第 18 节 测试保障 | 仓库含规则、指标、权限、AI、集成、流程、工作台等自动化测试 | `ione_qms/tests/` | `Static-verified`; `Runtime-unverified` | Frappe 依赖测试必须在 Bench CI 执行；临床金样本尚缺 |
| S19 | 第 19 节 路线图 | 一期所需平台底座大部分已编码 | 初始化蓝图、124 个 DocType、8 个 Workflow | `Implemented`; `Runtime-unverified` | 试点科室、阶段里程碑和退出标准需医院确认 |
| S20 | 第 20 节 组织预算 | 代码仓库不负责替代项目组织、人员和采购决策 | 无软件实现要求 | `External/Clinical blocker` | 医院须任命临床、质控、信息、运维、安全负责人 |
| S21 | 第 21 节 验收标准 | 可测试对象与验收清单已形成，尚无正式验收报告 | `docs/testing-and-acceptance.md`、本矩阵 | `Runtime-unverified` | 功能、数据、规则、AI、性能、恢复和管理效果均须签字 |
| S22 | 第 22 节 风险应对 | 默认关闭实时规则/AI，保留审计、对账、降级和回滚设计 | 运行设置、规则安全、AI 策略、发布回滚文档 | `Static-verified`; `Runtime-unverified` | 需演练误报、断链、越权、模型失败、回滚和恢复 |
| S23 | 第 23 节 建设结论 | 当前仅可称开发/QA 基线，不可称生产就绪 | 本矩阵及两份审计报告 | `Runtime-unverified` | 所有 P0 门槛关闭前维持 `NO-GO` |

### S09/S10/S13 governance addendum (2026-07-30)

- **S12 — AI production evaluation:** the former string/tool-name smoke
  evaluator was replaced by a ten-category typed gate. It executes
  model-requested calls through the live Flow schema and ordinary IONE
  policy/argument/scope/evidence path inside a commit-blocked, rollback-only
  synthetic tenant/task/session/run. Governed confusion matrices, signed and
  independently approved versioned thresholds, zero-tolerance security
  metrics, complete evidence/citation scope validation, and exact
  release/model/prompt/instruction/tool-schema/policy/KB/suite/threshold/code
  receipt bindings are implemented. Release/suite rows are locked and
  rechecked at execution and review. Status remains `Runtime-unverified` until
  the exact Press candidate completes real Qwen, rollback-residue, concurrent
  mutation, stale-binding, and all adversarial Bench tests.

- **S09 — Rule engine:** all seven governed test categories reset stale
  projections on definition change and require immutable receipts for the
  current rule checksum and executor commit. Historical replay remains an
  offline rollback-only artifact. Shadow Run is driven only by live events,
  emits append-only hashed execution/feedback receipts, has no clinical action
  path, and enforces a completed frozen observation window, reviewer coverage,
  zero unresolved safety defects, and an approved false-positive threshold.
- **S10 — Indicator engine:** Standard, Rule, and Indicator parents freeze
  semantic fields after review. Rule/Indicator versions bind one exact
  Standard Version and Clause, their checksum/content hash, authority snapshot,
  source provenance, and effective interval. Published indicator calculation
  uses only version snapshots and never mutable-parent fallback. A shared
  nine-dimension registry (with `medical_staff` canonicalized to `physician`)
  drives both publication and runtime capability validation. Exact input
  receipts bind version/period/dimensions, source-row manifests, mapping, query,
  and executable code. Result/detail history is append-only; an atomically
  locked current pointer drives facts, reports, and legacy APIs. Reproduction
  and migration quarantine fail closed when any lineage cannot be proved.
- **S13 — audit/security:** test executions, online-shadow executions,
  shadow feedback, and lineage migration receipts are append-only. Shadow
  executions use normal clinical-scope query/permission controls and are
  governed sensitive exports. Ambiguous reviewed legacy lineage is quarantined
  and blocks migration completion until reviewed retirement/replacement.
- **Status:** `Implemented`; `Static-verified`; `Runtime-unverified`. Required
  runtime evidence remains migrated DocTypes/indexes, Frappe lifecycle hooks,
  concurrent transition tests, live-event shadow traffic, scoped export
  negatives, and signed clinical acceptance on `manager`.

## 4. 九模块与主要对象

| 模块 | 主要对象/能力 | 当前判定 |
| --- | --- | --- |
| IONE Foundation | 组织、科室、人员、患者/就诊索引、系统设置、审计基础 | `Implemented`; `Static-verified`; `Runtime-unverified` |
| IONE Quality Standards | 标准/版本/条款、规则/版本/测试用例 | `Implemented`; `Static-verified`; `Runtime-unverified` |
| IONE Clinical Quality | 质控问题、证据、执行结果、结构化申诉与撤回 | `Implemented`; `Static-verified`; `Runtime-unverified` |
| IONE Improvement | 整改、复核、PDCA 与闭环 Workflow | `Implemented`; `Static-verified`; `Runtime-unverified` |
| IONE Indicators | 指标/版本、事实、快照、趋势与计算 | `Implemented`; `Static-verified`; `Runtime-unverified` |
| IONE Integration | 来源、端点、映射、消息、任务、数据质量与对账 | `Implemented`; `Static-verified`; `Runtime-unverified` |
| 集成租户边界 | Source/Endpoint 层级 allowlist、Patient/Encounter 医院分区键、Message/Event policy hash、越权无原文隔离 | `Implemented`; `Static-verified`; `Runtime-unverified` |
| IONE Flow AI | Agent、工具、策略、任务、候选发现、审批、执行修订与评测 | `Implemented`; `Static-verified`; `Runtime-unverified` |
| IONE Analytics | 角色工作台、趋势和管理分析入口 | `Implemented`; `Static-verified`; `Runtime-unverified` |
| IONE Administration | 集成/AI/安全设置、运行开关、安全事件上报 | `Implemented`; `Static-verified`; `Runtime-unverified` |

## 5. 工作流与闭环追踪

| 能力 | 实现证据 | 当前判定 | 待验收事项 |
| --- | --- | --- | --- |
| 标准、规则、指标版本治理 | `setup/workflows.py`、`services/versions.py` | 8 个 Workflow 已编码；`Static-verified` | Bench 中状态、动作、角色、并发和回退 |
| 问题确认、关闭与角色隔离 | `services/findings.py`、权限钩子 | `Static-verified`; `Runtime-unverified` | 真实角色和范围负向测试 |
| 结构化申诉与撤回 | `services/finding_appeals.py`、申诉 DocType/API/UI | `Implemented`; `Static-verified` | Desk/API/并发/审计运行验收 |
| 整改、复核、PDCA | `services/improvement.py`、改进 Workflow | `Static-verified`; `Runtime-unverified` | 超期升级、通知、管理评价和临床签字 |
| 安全事件上报 | `services/safety_events.py`、安全事件页面与任务 | `Implemented`; `Static-verified` | scheduler、通知收件人与处置闭环 |

## 6. 附录 C 七个核心 API

| API | 代码证据 | 当前判定 | 运行门槛 |
| --- | --- | --- | --- |
| `POST ione_qms.api.integration.receive_event` | `api/integration.py`、集成契约测试 | `Static-verified`; `Runtime-unverified` | 签名、幂等、重放、对账、限流 |
| `POST ione_qms.api.quality.evaluate_encounter` | `api/quality.py`、规则执行测试 | `Static-verified`; `Runtime-unverified` | 已发布规则、真实事件、事务与性能 |
| `GET ione_qms.api.quality.get_patient_alerts` | `api/quality.py`、临床范围权限测试 | `Static-verified`; `Runtime-unverified` | 患者级越权负向测试 |
| `POST ione_qms.api.improvement.submit_rectification` | `api/improvement.py`、改进流程测试 | `Static-verified`; `Runtime-unverified` | Workflow、角色、并发与通知 |
| `GET ione_qms.api.indicator.get_indicator_trend` | `api/indicator.py`、指标测试 | `Static-verified`; `Runtime-unverified` | 医院口径、事实装载和查询性能 |
| `POST ione_qms.api.ai.create_analysis_task` | `api/ai.py`、AI 运行护栏测试 | `Static-verified`; `Runtime-unverified` | AI 开关、Qwen 鉴权、脱敏、超时与成本 |
| `POST ione_qms.api.ai.review_candidate_finding` | `api/ai.py`、AI 审批并发测试 | `Static-verified`; `Runtime-unverified` | Flow 人工确认、权限、审计和续跑 |

## 7. 生产验收门槛

| 门槛 | 当前状态 | 必需证据 |
| --- | --- | --- |
| Bench/Frappe v17 | `Runtime-unverified` | `bench migrate`、安装/升级/回滚、Frappe 依赖测试日志 |
| Press 私有仓库访问 | `Runtime-unverified` | GitHub App 对 `ione_qms` 精确授权并可拉取固定 SHA |
| Press QA | `Not implemented` | 固定 SHA 的可重复构建、发布、健康检查和回滚记录 |
| `manager` 站点 | `Not implemented` | 安装记录、角色 UAT、关键 API 与后台任务验收 |
| 医院集成 | `External/Clinical blocker` | 源系统合同、字段字典、只读账号、Oracle 对账 query、日对账报告 |
| 临床内容 | `External/Clinical blocker` | 30–50 条批准规则、指标口径、金样本、回放结果、临床签字 |
| Qwen/Flow | `External/Clinical blocker`; `Runtime-unverified` | 内部鉴权、公共入口保护、消费者同步、脱敏、失败降级、固定评测集和人工确认报告；RAG 保持关闭 |
| 性能与灾备 | `Not implemented` | 当前根盘 86%；受控容量治理、压测、HA、监控告警、离站全量备份及恢复演练 |
| 调度任务 | `Runtime-unverified` | 当前 scheduler 异常；修复后验证队列、租约、重试、失败告警和补数 |

## 8. 总体判定

静态实现覆盖了方案要求的平台骨架、核心对象、工作流、API 和主要治理控制。

但 Bench、Press、`manager`、医院接口、临床内容、Qwen、scheduler、性能与灾备均未形成可审计闭环。

因此当前发布判定为 **NO-GO**；不得描述为“全部功能已实现”或“达到生产上线标准”。

## P105 / 11.5 source-document location supplement

| Requirement | Implementation evidence | Current verdict | Activation gate |
| --- | --- | --- | --- |
| Authorized one-click, read-only source document location | `services/source_document_locator.py`, `api/source_document.py`, `public/js/qc_finding_evidence.js`, `IONE Source Document Locator`, `IONE Source Document Access Log` | `Implemented`; `Static-verified`; `Runtime-unverified` | Exact HTTPS host/path contract, browser SSO, no-cross-host-redirect UAT, clinical scope negative tests, and Bench/browser acceptance are required. No approved locator means fail closed. |

## §6.2 / P118 / P127–P130 quality meeting supplement

| Requirement | Implementation evidence | Current verdict | Activation gate |
| --- | --- | --- | --- |
| Governed quality meeting, agenda, planned attendance, held evidence, and closure | `IONE Quality Meeting`, child agenda/attendee DocTypes, `services/quality_meetings.py`, `public/js/quality_meetings.js` | `Implemented`; `Static-verified`; `Runtime-unverified` | Hospital-approved meeting types, committee/attendee rules, quorum/notice policy, named approvers, evidence contract, and full Desk/API UAT |
| Separate independently approved minute and immutable derived decisions | `IONE Meeting Minute`, `IONE Meeting Decision`, meeting-level lock, unique approved-meeting key, checksums | `Implemented`; `Static-verified`; `Runtime-unverified` | Bench transaction/concurrency tests, creator/approver and wrong-scope negative tests, signed minute/decision acceptance |
| Accountable action, overdue queue, evidence, effect result, and independent verification | `IONE Quality Action Item`, append-only `IONE Quality Action Verification Round`, checksum chain and idempotency keys, `IONE Quality Action Workbench`, `tasks/quality.py`, POST-only action APIs | `Implemented`; `Static-verified`; `Runtime-unverified` | Approved owner/verifier matrix, Bench transaction/retry/concurrency and chain-tamper tests, due/escalation SLA and recipients, scheduler/notification UAT, effect/closure acceptance |
| De-identified, versioned, independently published experience sharing | `IONE Quality Experience Share`, recursive metadata/identifier guard, publication lock, active-slot/version/checksum fields | `Implemented`; `Static-verified`; `Runtime-unverified` | Hospital de-identification policy/dictionary, privacy review, leakage tests, effective/retirement policy, publication concurrency UAT |
| No generic deletion/native export/print of governed meeting records | DocPerm metadata, `hooks.py`, `services/data_export.py`, `overrides/native_export_guard.py`, deletion guard | `Implemented`; `Static-verified`; `Runtime-unverified` | Bench role matrix, list/report/download/print negative tests and controlled-export acceptance |

## §12.3 / §12.5 AI-to-meeting human-control supplement

| Requirement | Implementation evidence | Current verdict | Activation gate |
| --- | --- | --- | --- |
| Monthly/quality Agent output remains a report draft and may only be referenced as meeting material | `IONE AI Report Draft`, agenda `material_type`/`ai_report_draft`, report status/scope/checksum validation | `Implemented`; `Static-verified`; `Runtime-unverified` | Approved Qwen release/policy, human report review, exact-scope UAT, checksum-tamper negative test |
| Terminal monthly report recovery preserves the failed task/month and requires the exact independent reviewer | `IONE AI Report Recovery Authorization`, `services/ai_report_schedules.py`, POST API, task recovery provenance, two-authorization cap, atomic task/runtime/enqueue registration, pending-task repair and sanitized incident | `Implemented`; `Static-verified`; `Runtime-unverified` | Migrated-Bench concurrency/idempotency, scheduler/queue/commit failure injection, Qwen replacement-run, incident and role-negative browser UAT |
| Aggregate monthly output cannot bypass the draft tool safety gate | `ai/output_safety.py`, `ai/output_quarantine.py`, initial/Resume/orphan orchestration, content-free Flow quarantine and privacy incident | `Implemented`; `Static-verified`; `Runtime-unverified` | Pinned-Flow tests for no-tool final output, malformed/unknown tool calls, provider-controlled call IDs, transaction crash recovery, zero retained direct identifiers and privacy/security sign-off |
| AI cannot approve or finalize meeting, minute, decision, action, or experience publication | Named human actor checks, separation-of-duties services, no Agent Service generic DocPerm, POST-only APIs | `Implemented`; `Static-verified`; `Runtime-unverified` | Bench/Flow prompt-injection and unauthorized-tool tests with zero successful bypasses |

The detailed state/evidence contract is documented in
`docs/quality-meetings-and-actions.md`. None of these static results substitutes
for hospital governance inputs, migrated-Bench verification, Press QA, or
`manager` role acceptance.
