# IONE QMS — Frappe v17 / Flow develop 运行时审计

审计日期：2026-07-29；运行基线复核：2026-07-31。

## 1. 审计结论

当前代码按 Frappe v17 develop 与 Frappe Flow develop 的模型和 Hook 方式设计，静态证据支持其作为 Bench 验证候选。

已逐项复核的源码基线为 Frappe
`0a1eb8e38babeccfcd2e1845efa04b376a63366f`、Flow
`4ac02293f656d9a979214c62b168512b5b6d008e`，并核对 `manager` 当前 Frappe
快照 `5aef9956c7bf58d36cfd59e2242cc1a26d385ea2`。QMS 对 Frappe/Flow
的源码 API 适配未发现未关闭的确定 P0/P1；这不替代数据库、其他 bench
应用和目标站点运行测试。

目标站点当前 Drive 基线为
`cd3438d1ab0b0fc1b8c10e282639ec0bd2ee7d82`。QMS 使用
`extend_doctype_class` cooperative mixin，使有效 MRO 在有 Drive 时为
QMS mixin → Drive File → Frappe File，无 Drive 时为 QMS mixin → Frappe
File。两种静态/MRO 合成测试已通过，真实 Drive 生命周期仍由 manager CI
和 Press QA 验收。

但当前没有完整 Bench、Press QA 或 `manager` 站点运行记录，也没有医院基础设施和临床验收证据。

**运行时判定：NO-GO。**

“元数据可校验、测试可通过”不等于“Frappe 运行兼容”，更不等于“生产安全”。

## 2. 当前静态基线

| 项目 | 当前事实 | 判定 |
| --- | --- | --- |
| 自研应用 | 单一 `ione_qms` | 已编码 |
| Module | 9 个 | 静态可核对 |
| Workspace | 9 个 | 静态可核对；Desk 未验证 |
| DocType | 124 个 | 元数据可校验；数据库迁移未验证 |
| Workflow | 8 个 | 配置及保护逻辑已编码；真实工作流未验证 |
| Flow Agent | 5 个蓝图 | 可初始化设计；Flow 运行未验证 |
| 核心 API | 7 个方案 API 均有实现 | 契约可静态验证；HTTP 运行未验证 |
| 实时规则 | 生产开关默认关闭 | 安全默认值正确；启用验收未完成 |
| AI | 总开关默认关闭 | 安全默认值正确；Qwen/Flow 未验收 |
| 代码交付 | 尚无与成功远端 CI/Press 制品绑定的不可变发布证据 | 不能作为发布基线 |

## 3. Frappe v17 静态兼容证据

| 检查面 | 静态证据 | 剩余运行风险 |
| --- | --- | --- |
| DocType JSON | 字段、Link/Table 目标、权限及模块可由元数据校验器检查 | `bench migrate` 建表、索引、默认值和补丁顺序 |
| Hook 路径 | `hooks.py` 中任务、权限、DocType 事件目标可导入式核对 | scheduler、worker、请求上下文和事务行为 |
| Controller | DocType controller 与 service 分层已建立 | `validate/on_update/on_submit` 顺序及回滚 |
| Workflow | 8 个 Workflow 和服务端防绕过逻辑已编码 | Frappe `apply_workflow`、角色缓存、并发和通知 |
| 权限 | DocType 权限、范围查询、API 检查和受控导出已编码 | Desk 列表、报表、Link 搜索、导出和 REST 负向测试 |
| 版本唯一性 | 父版本同步和单一激活版本约束已编码 | MariaDB 唯一索引、并发激活和迁移回填 |
| API | 白名单方法、参数校验和服务调用路径已编码 | CSRF/认证、限流、事务、异常格式和代理超时 |
| 后台任务 | 集成、安全、规则、指标任务已挂接 | 队列选择、幂等、重试、超时、失败告警和补数 |
| Workspace | 九个 Workspace JSON 与角色工作台查询存在 | Desk 构建、缓存、角色可见性、查询性能 |
| File 控制器 | QMS 使用 cooperative extension，不替换 Drive/core 控制器 | Drive team/site 文件 CRUD、权限、删除清理及多站点 controller cache |

静态验证结果只能作为进入 Bench CI 的门槛，不能替代 Bench CI。

## 4. Flow develop 与 Qwen

| 检查面 | 当前实现 | 仍需验证 |
| --- | --- | --- |
| Agent 蓝图 | 5 个 Agent 的身份、策略和最小工具已编码 | Flow DocType 创建、字段兼容和版本升级 |
| 工具权限 | 应用侧限制写入、审批和临床范围 | Flow 通用入口、Imported Tool、Desk 访问的负向测试 |
| 人工确认 | 初始/Resume claim、审批消费、并发 CAS 和多修订审计已编码 | 暂停、确认、拒绝、崩溃窗口、续跑、超时和重试 |
| AI 开关 | 系统设置与 AI 设置默认关闭 | 所有 API、任务、Trigger 和工具均实际受开关约束 |
| Qwen 接入 | 目标实例存在，配置与调用边界有设计 | 内部鉴权、凭据轮换、公共入口保护、模型名、TLS、超时、并发和 SLA |
| 数据治理 | 脱敏、最小权限和审计路径有实现 | 真实提示词/响应无敏感泄露，日志和缓存边界 |
| 评测 | Agent 评测对象和框架已编码 | 固定评测集、阈值、回归报告和临床批准 |
| 降级 | AI 失败不应改变确定性结论 | 网络中断、限流、模型错误和不可用演练 |

在上述项目通过前，不得启用 AI 或 Flow Trigger。

## 5. Bench CI 必测清单

| 阶段 | 必测项 | 通过证据 |
| --- | --- | --- |
| 环境锁定 | 固定 Frappe、Flow、`ione_qms` commit SHA | CI 清单和构建元数据 |
| 干净安装 | 新站点安装 `ione_qms` | 安装日志、124 个 DocType、8 个 Workflow、9 个 Workspace |
| 重复迁移 | 连续执行 migrate 无漂移 | 两次迁移日志和 schema diff |
| 升级迁移 | 从上一基线升级 | 补丁、回填、唯一约束和数据校验报告 |
| Frappe 测试 | 运行依赖数据库、权限、Workflow 的测试 | Bench 测试日志，无跳过关键测试 |
| API 烟雾 | 7 个核心 API 的认证、成功和失败路径 | HTTP 结果、审计记录和错误契约 |
| 权限负向 | 医生、科室质控、职能质控、管理员、AI 用户 | Desk/API/报表/导出/Link 搜索越权全部拒绝 |
| Workflow | 8 个流程的合法/非法转换、自审批和并发 | 状态、审计、通知和事务记录 |
| scheduler | 定时任务、队列、重试、幂等、告警 | 执行历史、失败注入和恢复记录 |
| 规则安全 | 默认关闭、Shadow、正式启用和紧急停用 | 设置、执行结果和回滚记录 |
| AI 安全 | 默认关闭、开关覆盖、工具限制、人工确认 | Flow/Qwen 全链路和负向报告 |

任何关键测试因缺少 Frappe 依赖而跳过，都不能计为通过。

## 6. Press QA 与 `manager` 验证

| 阶段 | 当前状态 | 必需证据 |
| --- | --- | --- |
| 自有仓库 | 尚无已通过远端 CI 的候选 SHA | develop 分支 commit、评审记录和远端可见性 |
| Press 源访问 | 私有仓库 GitHub App 权限未证明 | 固定 SHA 拉取成功且无多余仓库授权 |
| Press App | 未验证 | 从固定 SHA 构建成功，依赖版本可追溯 |
| QA 站点 | 未验证 | 安装、迁移、登录、Workspace、API、任务和回滚 |
| 发布前备份 | 未验证 | 数据库、public/private files、site config、密钥清单 |
| `manager` 安装 | 未执行 | 变更单、备份、安装日志、迁移日志和健康检查 |
| 生产前 UAT | 未执行 | 角色、流程、规则、指标、集成、AI 和审计签字 |

Press 只能部署已提交、已测试并可回滚的不可变版本；不得直接部署工作树。

## 7. 医院基础设施阻断

| 阻断项 | 当前缺失 | 运行影响 |
| --- | --- | --- |
| 源系统网络 | 防火墙、路由、DNS、证书、白名单 | 无法验证接入、超时和重连 |
| Oracle 只读权限 | 账号、视图、最小权限证明 | 无法验证真实增量和零写入 |
| 对账 query | 来源总量、缺失、重复、延迟 SQL | 无法证明数据完整性 |
| Qwen 鉴权 | 实例存在但内部鉴权、公共入口保护和消费者同步未闭合 | 无法接受 AI 调用 |
| SMTP/通知 | 发信账号、域名、收件规则 | 无法验证提醒和升级 |
| 对象存储/文件 | 持久化、加密、生命周期 | 无法验证证据与附件安全 |
| 监控平台 | 指标、日志、追踪、告警路由 | 无法证明可观测性和响应 |
| 离站备份 | 隔离位置、保留、加密、密钥托管 | 无法满足灾难恢复 |

## 8. 性能、可用性与恢复

以下证据目前均不存在，状态为 `NO-GO`：

- 代表性数据规模下的并发、吞吐、P95/P99 与资源曲线；
- 规则批量回放、指标汇总、角色工作台和导出的性能报告；
- Web、worker、Redis、MariaDB、文件存储的故障切换演练；
- 队列积压、接口延迟、失败率、数据质量和安全事件告警；
- 数据库、站点文件、私有附件、配置与密钥的离站全量备份；
- 隔离环境恢复演练、数据一致性校验及经批准的 RPO/RTO。

目标环境只读预检还记录了以下硬门禁：构建主机根盘使用率 86%；最近
备份没有离站/物理证据且文件不可用；scheduler 当前退出；`manager` 与
`screening` 共享 active bench；Press 对私有仓库的读取尚未证明。候选必须
由 Press 只拆分 `manager`，并证明 `screening` 保持原 bench。

同一预检发现该固定 Drive 版本的 team-file `validate()` 存在既有不可达
校验代码。QMS 不修改或替代 Drive；该风险须由 Drive 责任方修复、升级或
形成正式接受记录，不能由本项目静默重置其已有工作树。

## 9. 运行启用原则

1. 实时规则和 AI 保持默认关闭。
2. 先完成 Bench CI，再进入 Press QA。
3. Press QA 通过后，才允许申请 `manager` 安装。
4. 医院接口、临床规则和 Qwen 必须分别取得责任方批准。
5. scheduler、监控、备份和恢复演练必须在生产启用前完成。
6. 任一 P0 缺少证据时，发布决策保持 NO-GO。

## 10. 最终判定

当前仓库具备进入 Frappe v17/Flow develop Bench 验证的静态基础。

当前仓库不具备 Press 生产发布条件，也不具备安装到 `manager` 后直接启用临床规则、集成或 AI 的条件。

下一验收阶段应是：固定 SHA 的 Bench CI → Press QA → 医院/临床联调与 UAT → 性能和恢复演练 → 生产变更评审。
