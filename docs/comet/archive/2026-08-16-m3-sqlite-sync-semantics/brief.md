# Outcome

为用户主动选择的定向讨论增加本地 SQLite 持久化：按 viewer 与 discussion 保存跨运行观察、完整同步基线和同步运行账本，并以保守、可恢复、可审计的语义计算新增观察与当前不可见变化。完整同步可以推进可见性 baseline；不完整同步只能吸收新观察，不能推断缺失、删除或不可用。

# Scope

- 在 Adapter 之外新增 SQLite 存储边界与同步语义层；Adapter 继续只负责平台读取和规范化事实。
- 持久化 viewer、viewer-independent discussions、comments、discussion_viewer_state、comment_observation、sync_runs，以及 M4/M6 后续需要但本阶段不执行业务行为的 notification_sync_state、reply_events、outbound_replies/outbox schema。
- 每次持久化同步记录 observed_ids、complete 与 diagnostics；按 `(viewer_id, discussion_id)` 维护 ever_seen 与 last complete baseline。
- 完整同步计算并原子提交 current visible、newly observed、not currently visible 与新 baseline；不完整同步仅提交已观察事实和诊断。
- 提供跨运行、跨小时、跨讨论的只读查询能力，供后续上下文与通知里程碑复用。
- 将 M3 接入现有定向讨论 CLI；legacy 整视频诊断路径不进入产品持久化。
- 更新相关测试、README、architecture、project 与 roadmap 的当前状态。

# Non-goals

- 不读取或发现通知，不生成 reply event，不实现通知分页或事件去重业务。
- 不调用 LLM、不组装 M5 模型上下文、不推断参与者画像或意图。
- 不发送评论，不实现 Writer、确认流程、真实 outbox 状态转换或写接口。
- 不持久化 Cookie、CSRF、headers 或其他凭证；不新增跨运行认证状态。
- 不把 legacy `comments.json` 或其他 JSON 导出迁移为产品数据。
- 不持久化平台抓取游标，也不实现页级断点续抓；失败运行由下一次同步从头重读。
- 不持久化树结构、conversation chains 或 `is_self`；这些仍由规范化评论事实与 viewer 在输出/查询阶段派生。

# Acceptance examples

- A1：schema 与事务约束生效；discussion 自然键和 `(discussion_id, platform_comment_id)` comment 唯一键不会产生重复实体，同一同步中的 comments、observations、sync_run 与 baseline 要么全部提交，要么全部回滚。
- A2：在事务中途注入异常并重新打开数据库后，上一轮已提交 baseline 保持完整，数据库可继续同步，失败候选不留下半份运行或可见性差集。
- A3：同一 viewer 的连续完整同步正确计算 `newly_observed = observed_ids - ever_seen_before`，把 `previous_visible_ids - observed_ids` 仅标为 `not_currently_visible`，并把 last complete baseline 更新为本轮 observed_ids。
- A4：`complete=false` 时，新观察仍并入 ever_seen 且 diagnostics/sync_run 可审计，但不替换 last complete baseline、不计算缺失差集、不写任何删除或当前不可见推断。
- A5：完整且 observed_ids 为空时，新 baseline 为空，上一完整 baseline 的评论均转为 `not_currently_visible`；这仍不证明永久删除。
- A6：同一 viewer 对同一无变化快照重复完整同步时，第二轮 newly_observed 和 visibility diff 为空，实体与 observation 不重复，追加的 sync_run 仍可审计。
- A7：同一 discussion 的不同 viewer 具有独立 state、baseline 与 comment observations；username 展示值变化不改变 viewer 稳定身份。
- A8：`comment_observation.current_visibility` 在尚无完整 baseline 可作结论时为空；非空值只允许 `visible / not_currently_visible`。`unavailable` 只允许作为 `reply_event.target_availability`，并与事件状态正交。
- A9：comments 保存规范化关系、作者、内容与展示事实，不保存 `is_self`、单一全局 visibility 或 Cookie/CSRF/平台请求线协议字段；平台 comment ID 无截断或精度损失。
- A10：至少两个跨小时同步的 discussions 可按 viewer/discussion 稳定查询各自 sync runs、ever seen、last complete 与 current observation；查询结果不串 viewer 或 discussion。
- A11：旧 `comments.json` 不被自动发现或迁移，唯一测试凭证不出现在数据库、stdout、文件、stderr、异常、diagnostics、Runtime handoff 或报告。
- A12：未启用持久化时，现有定向 schema 1.2、legacy schema 1.0、输出文件覆盖保护、请求范围及 CLI exit 0/1/2 不回归；全量离线测试、ruff format/check 与覆盖率检查通过。
- A13：启用持久化时，成功或远端 `complete=false` 的同步仍输出不扩展字段的 schema 1.2 并分别返回 exit 0/2；legacy reference + `--database` 在持久化前 exit 1 且无 JSON/DB 写入；SQLite 打开、schema、锁定、约束或 commit 失败时整单元回滚、exit 1 且无 JSON。

# Constraints and invariants

- discussion identity 固定为 `(platform, object_type, oid/aid, root_comment_id)`，不包含 viewer 或 focus；viewer-relative 状态只存在于 discussion_viewer_state 与 comment_observation。
- 一个同步运行的持久化原子单元包含规范化实体、observations、sync_run 和（仅 complete 时）baseline/diff；并发运行不得以陈旧 baseline 覆盖较新提交。
- 开始本轮前的 `previous_visible_ids` 必须来自该 viewer/discussion 最近一次完整同步；不完整运行永远不能成为后续 diff baseline。
- `ever_seen` 单调增长；`current_visibility` 不是永久删除证明；通知 feed 缺席不属于 M3 可见性输入。
- comments 中的占位/未知字段不得覆盖后续或既有的更完整事实；树、孤儿诊断与输出派生保持现有语义。
- 使用 Python 标准库 sqlite3，不为 M3 引入 ORM；具体 DDL、WAL、schema version 与索引属于实现选择，但必须满足原子性、恢复和无精度损失要求。
- 本 change 保持单一 Native change：storage、sync 与 CLI 共享同一事务/基线契约，拆成独立 children 没有真实独立验收价值。

# Decisions

- M3 只持久化用户主动提供评论分享链接所选择的定向讨论；legacy 整视频读取继续只是无持久化的诊断兼容路径。
- Adapter 保持平台读取边界；SQLite 与 baseline/diff 逻辑放在独立 storage/sync 层，避免平台协议与本地账本耦合。
- 旧 JSON 导出不作为产品数据迁移源；M3 从 SQLite 新 schema 开始。
- reply_events、notification_sync_state 与 outbound_replies/outbox 在 M3 只建立可约束、可迁移的存储基础，不提前实现 M4/M6 行为。
- 持久化由显式 `--database PATH` 启用；未提供该参数时现有行为保持不变。第一次成功提交数据库事务即自动建立该 viewer 与 discussion 的 tracked/bound 状态，与本轮 `complete` 无关，不另设追踪命令。
- anonymous 定向同步允许持久化。每个数据库对每个平台使用一个稳定 anonymous viewer 实体，其 `platform_user_id` 为空；它与所有 authenticated mid 隔离并独立维护 baseline。
- 定向 JSON 输出继续使用 schema 1.2，不加入 sync run 或 diff 字段；M3 通过稳定的 Python storage/query API 提供跨运行查询，最终用户查询 CLI 留给后续里程碑。
- 显式启用持久化后，SQLite 打开、schema、锁定、约束或提交失败均 fail closed：事务回滚、CLI exit 1，stdout 与目标输出文件均不产生本轮 JSON。

# Open questions

- 无。

# Verification expectations

- Builder 开发期至少运行新增 storage/sync/CLI 定向测试，并在候选交接前运行完整离线测试。
- Runtime Verify 运行 `uv run ruff format --check .`、`uv run ruff check .` 与 `uv run pytest -q --cov=auto_comment_reply --cov-report=term-missing`。
- 测试使用临时 SQLite 文件和故障注入覆盖唯一约束、事务回滚、重开恢复、完整/不完整 baseline、幂等、匿名/登录 viewer 隔离、跨小时多讨论查询及凭证泄漏。
- Verifier 必须逐项核对 A1-A13，并检查长期文档只陈述已实现、已验证的当前事实。
