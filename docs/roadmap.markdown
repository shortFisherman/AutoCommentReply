# 路线图：从 legacy 基线到上下文与回复助手

> 本文档描述 M4+ 的**计划**，不构成实施授权；M1/M2/M3 的完成状态与验证记录见下文。当前实现架构见 [architecture.markdown](architecture.markdown)（当前项目画像），项目意图见 [project.markdown](project.markdown)，当前可运行代码的完整现状见 architecture。

## 状态

- **M1（输入与讨论定向读取）：已完成**——讨论定向读取是当前 MVP 的基础；legacy 整视频只读（旧 MVP）保留为诊断兼容，不是目标产品。
- **M2（本地认证与 viewer 身份）：已完成**——正式需求、规格与验收历史由 Comet change `local-auth-viewer-identity` 承载；验证记录见下文。
- **M3（SQLite 持久化与同步语义）：已完成**——已接入当前 MVP；正式需求、规格与验收由 Comet change `m3-sqlite-sync-semantics` 承载；实现要点与最终安全门证据见下文。
- M4–M7 均**未实现**。
- 任何后续编码必须先有明确的用户授权或新的 Comet change，再开始实施；roadmap 本身不授权提前编码，也不允许跨阶段偷跑。

## 迁移总原则

- 每个里程碑独立可验收；最低验收即安全门。
- 目标能力不得在迁移前写进当前代码；legacy 全量抓取 CLI 可继续用于诊断，直到 M7 做出退役/保留的明确决定。
- B 站私有接口细节以捕获或最小授权实测为准；本路线图不预设未经证实的接口契约。

## 依赖顺序与里程碑

### M1 输入与讨论定向读取（已完成）

- **目标**：支持手机分享链接（b23.tv 或展开后的 URL）作为入口；解析 `comment_root_id`、`comment_secondary_id`、`#reply<id>`；忽略 `share_tag / unique_k / vd_source` 等追踪参数；规范化 `(platform, object_type, oid/aid, bvid, root_comment_id, focus_comment_id)`。
- b23 解析：跟随 30x 的 `Location` 最多 5 跳，每一跳先验证（拒绝循环、畸形、非 http(s)）；链上第一个非 b23 目标必须是允许的 Bilibili 域名，外站立即拒绝，防止任意 URL/SSRF 入口；拿到允许的非 b23 `Location` 后直接解析 query/fragment，尽量不请求最终视频页面。
- 定向同步：只抓指定根评论及其当前所有可见楼中楼回复；不再翻整段视频根评论列表。
- **前置依赖**：现有 Adapter 的解析/建树能力（保留复用）。
- **最低验收（安全门，已逐项验证）**：
  - 同一输入两次运行得到同一讨论身份 `(bilibili, video, oid, root_comment_id)`，且与 focus/viewer 无关——已验证（离线测试）。
  - `focus_comment_id` 不作为 parent/root 使用、不改变同步范围——已验证（离线测试）。
  - b23 链最多 5 跳，循环/畸形/非 http(s)/userinfo/危险端口/外站目标被拒绝，第一个非 b23 目标必须是允许的 Bilibili 域名，终态 HTML 不请求——已验证（离线测试）。
  - 定向读取请求量只与楼中楼页数相关，不出现主评论翻页（`root_pages_fetched=0`），不调用 nav/WBI/main（M1 验收时；M2 起认证定向新增一次可与 legacy WBI 共用的 nav 身份请求）——已验证（离线测试 + 真实 smoke）。
  - 缺 `comment_root_id` 或 focus 冲突 fail closed，不退回全量；根无效时 `complete=false` 且不声称永久删除——已验证（离线测试）。
  - 输出 schema 1.1（M1 验收时为 1.1；M2 已按用户确认将定向输出升级为 schema 1.2）；`complete/diagnostics/exit 0/1/2` 语义与 legacy 一致——已验证。
  - M1 验收基线（当时）：`129 passed`、覆盖率 89%；匿名真实 CLI smoke：1 根评论 + 1 回复、`root_pages_fetched=0`、`complete=true`。
- **非范围**：SQLite、认证身份、通知、LLM、写接口。

### M2 本地认证与 viewer 身份（已完成）

- **目标**：本地 authenticated session + 当前 viewer 身份（`platform_user_id` / B 站 mid），而不是只有 Cookie 文本；用户名仅展示；凭证不暴露给模型、不写进日志。
- **实现要点**：
  - 认证输入沿用 `--cookie-file`（本机私有文件，优先）与 `BILIBILI_COOKIE` 环境变量；无 `auth.json`、`--auth-file`、默认凭证路径或跨运行认证状态；凭证只存在于进程内与发往允许 Bilibili 主机的 Cookie header；操作者只向 Agent/模型提供凭证文件路径与任务输入，不提供 Cookie 内容。
  - 无凭证 → 显式 anonymous viewer（`authenticated=false`、`platform_user_id=null`、`username=null`），不因身份请求 nav；有凭证 → 评论读取前一次 `GET /x/web-interface/nav` 解析 `isLogin=true` + 正整数 mid（uname 仅展示、可空），在同一 Adapter 生命周期内缓存并与 legacy WBI 取 key 共用。
  - 身份无法确认（nav 未登录 / mid 非法 / 结构无效 / 请求失败）→ fail closed，CLI exit 1 且 stdout/磁盘均无 JSON，不静默降级为匿名。
  - 定向输出升级 schema 1.2：顶层 `viewer`；`comments`/`trees` 只输出 `author_id` + 三态 `is_self`，不保留 `user_id` 兼容别名；`is_self` 输出期派生，不入 Comment 事实模型。legacy schema 1.0 不变。
  - 讨论 identity `(platform, object_type, oid, root_comment_id)`、focus 规则、同步范围与 `root_pages_fetched=0` 不随 viewer 变化；定向路径仍不调用主评论 `main`、不做 WBI 签名。
- **前置依赖**：M1（输入与定向读取链路，已完成）。
- **最低验收（安全门，已逐项验证）**：
  - A1 匿名显式 viewer 且不为身份请求 nav——已验证（离线测试：定向路径无 nav 请求）。
  - A2 登录 viewer 经一次 nav 解析并缓存，与 legacy WBI 共用同一响应；同一 Adapter 生命周期内多次读取 nav 计数为 1——已验证（离线测试）。
  - A3 `is_self` 三态：作者=viewer → `true`、作者已知且不等 → `false`、viewer 匿名或作者未知 → `null`；不存入 Comment 事实模型——已验证（离线测试）。
  - A4 提供凭证但 nav 未登录/`isLogin` 缺失/`mid` 缺失或非法/结构无效 → 类型化认证/解析错误，CLI exit 1、无 JSON，评论读取不开始——已验证（离线参数化测试，12 种非法形态）。
  - A5 同一评论链接在匿名与任意登录 viewer 下 discussion identity 与同步范围一致；定向路径不调用 `main`、不做 WBI 签名，`root_pages_fetched=0`——已验证（离线测试）。
  - A6 唯一标记测试凭证不出现在 JSON、文件、stderr/verbose、异常、repr、diagnostics/details 或文档——已验证（离线泄漏路径断言）。
  - A7 legacy schema 1.0 输出契约与 fail-closed、complete、diagnostics、exit 0/1/2 语义不回归——已验证（离线测试）。
  - A8 验证基线：`uv run ruff format --check .` 与 `uv run ruff check .` 通过；`uv run pytest --cov=auto_comment_reply` 为 **168 passed**、总覆盖率 **90%**。
  - 真实只读核验：2026-08-16 匿名 nav 返回 `code=-101`、`isLogin=false`、`mid=null`、`uname=null`，且仍含可用的 `wbi_img`；登录态**只**由脱敏离线 fixture 验证（`tests/_helpers.py`、`test_viewer.py`、`test_output.py`、`test_cli.py`），未使用真实私人账号 smoke，不伪称已做真实登录验证。
- **非范围**：通知读取、SQLite 全量迁移、写接口。

### M3 SQLite 持久化与同步语义（已完成）

- **目标**：只持久化用户选中的定向讨论，由显式 `--database PATH` 启用（无默认路径、无环境变量、无自动发现、无最终用户查询 CLI）。schema v1 实体：`viewers`（`platform_user_id` 稳定身份；每平台每库恰一个 anonymous）、`discussions`（自然键 `(platform, object_type, oid, root_comment_id)`，与 viewer/focus 无关）、`comments`（关系、作者、内容；唯一 `(discussion_id, platform_comment_id)`，不含 `is_self`/单一 visibility/线协议字段）、`viewer_state`（即 discussion_viewer_state：`(discussion_id, viewer_id)`、绑定/追踪状态、`last_complete` baseline）、`comment_observation`（`(discussion_id, viewer_id, comment_row_id)`、first_seen/last_seen、current_visibility 可空且仅 `visible / not_currently_visible`）、`sync_runs`（viewer + discussion、observed_ids、complete、diagnostics）、以及 M4/M6 存储基础 `notification_sync_state`、`reply_events`（独立 `target_availability`：unknown / available / unavailable，与事件状态正交）、`outbound_replies`。
- **同步语义**：每次 sync run 记录 `observed_ids`；对 `(viewer_id, discussion_id)` 维护 ever-seen 与 `last_complete_visible_ids`；开始本轮前 `previous_visible_ids = last_complete_visible_ids`；无论 complete 与否，`newly_observed = observed_ids − ever_seen_before` 后并入 ever_seen；`complete=true` 时 `current_visible_ids = observed_ids`、`not_currently_visible = previous − current` 并更新 baseline，可见性只写 `visible / not_currently_visible`、差集只能推进 `not_currently_visible` 且不证明删除；`complete=false` 时**不替换 baseline、不计算缺失/删除/当前不可见差集**，只保留新观察与诊断；`unavailable` 不是 `current_visibility` 的值，而是 `reply_event.target_availability` 的值，不证明永久删除。
- **实现要点**：
  - 标准库 `sqlite3`，无新运行时依赖；显式 schema version；WAL、5 秒 busy timeout、外键约束开启；写事务 `BEGIN IMMEDIATE` 串行化 writer；未知/更新 schema fail closed；一个 sync run 的原子单元包含 viewer/discussion/comment facts、observations、sync run 与（仅 complete 时）baseline/diff。
  - relationship conflict 审计：同轮重复或与已存事实冲突的 root/parent 关系不被覆盖，追加固定脱敏 error diagnostic、整轮降级 `complete=false`、不替换 baseline、不计算 diff；持久化 `complete`/`diagnostics` 与最终 schema 1.2 输出文档完全一致（sync 层以最终文档为权威，不读 `result.complete`）。
  - 查询 API：`find_viewer` / `find_discussion` / `list_viewer_discussions` / `get_viewer_discussion_state` / `list_sync_runs` / `list_comments` / `list_observations`（只读、viewer 显式隔离、稳定排序）。
  - legacy 引用 + `--database` 在持久化前 exit 1（无 JSON/DB 写入）；SQLite 致命失败整单元回滚、exit 1、无 JSON；远端/结构 `complete=false` 但 DB 事务成功时按不完整同步落库并输出 schema 1.2、exit 2。
- **前置依赖**：M2（viewer 身份，已完成）。
- **最低验收（安全门，已逐项验证——全部为离线自动测试）**：

  | 验收项 | 证据（`tests/`，2026-08-16 主工作区实测） |
  | --- | --- |
  | A1 schema 与事务约束生效；自然键与 comment 唯一键不产生重复实体；comments/observations/sync_run/baseline 全有或全无 | `test_storage.py` 唯一键/约束用例 + `test_sync.py` 事务故障注入回滚用例 |
  | A2 事务中途异常后重开数据库：上一轮 baseline 完整、数据库可继续同步、无半份 run | `test_sync.py::test_fault_before_commit_rolls_back_entire_run` 及重开断言 |
  | A3 连续完整同步正确计算 `newly_observed`、`not_currently_visible` 并更新 baseline | `test_sync.py::test_complete_runs_compute_newly_observed_and_visibility_diff` |
  | A4 `complete=false` 吸收新观察、可审计，但不替换 baseline、不写删除/不可见推断 | `test_sync.py::test_incomplete_run_absorbs_observations_without_baseline_change` |
  | A5 完整且 observed_ids 为空：新 baseline 为空、上一基线全部转 `not_currently_visible` | `test_sync.py::test_empty_complete_run_clears_baseline_conservatively` |
  | A6 同一无变化快照重复完整同步：第二轮无 newly_observed/diff、实体不重复、run 追加 | `test_sync.py::test_identical_snapshot_is_idempotent` |
  | A7 不同 viewer 独立 state/baseline/observations；username 变化不改变身份 | `test_sync.py::test_viewers_are_isolated_and_username_change_keeps_identity` |
  | A8 `current_visibility` 可空且仅 `visible/not_currently_visible`；`unavailable` 只属于 `reply_event.target_availability` 且与事件状态正交 | `test_storage.py::test_observation_visibility_check_rejects_unavailable`、`test_reply_event_target_availability_is_independent_of_status` |
  | A9 comments 保存规范化事实、无 `is_self`/全局 visibility/线协议字段；平台 ID 无精度损失 | `test_storage.py::test_comments_store_no_is_self_or_global_visibility`、`test_large_int64_ids_round_trip_without_precision_loss` |
  | A10 跨小时多讨论按 viewer/discussion 稳定查询，不串 viewer/discussion | `test_storage.py::test_query_api_is_viewer_scoped_and_stably_ordered` |
  | A11 旧 `comments.json` 不被自动发现/迁移；唯一测试凭证不出现于 DB 字节、stdout、文件、stderr、异常、repr、诊断 | `test_storage.py::test_credentials_never_persist_to_database_bytes` 与 CLI 泄漏路径用例 |
  | A12 未启用持久化时 schema 1.2/1.0、覆盖保护、请求范围、exit 0/1/2 不回归 | `test_cli.py::test_cli_without_database_never_calls_persistence` + 全量 210 passed |
  | A13 启用持久化：成功/远端不完整仍输出 schema 1.2 并分别 exit 0/2；legacy+`--database` 持久化前 exit 1；SQLite 打开/schema/锁/约束/提交失败整单元回滚、exit 1、无 JSON | `test_cli.py` 的 `test_cli_database_*`、`test_cli_legacy_reference_with_database_exits_one_before_persist`、`test_cli_database_persistence_error_exits_one_without_json_or_file` 等 |

  spec 级验收 A14–A84 由 Comet change 承载；长期文档记录 brief 安全门 A1–A13 的最终证据。

- 自动测试基线（2026-08-16 主工作区实测）：`uv run pytest -q --cov=auto_comment_reply --cov-report=term-missing` → **210 passed**、总覆盖率 **87%**；`uv run ruff format --check .` 与 `uv run ruff check .` 均通过。
- **验证边界**：M3 验收全部由离线自动测试完成（`httpx.MockTransport` + 临时 SQLite 文件）；**没有做真实私人账号 smoke，也没有对 `--database` 做过真实网络 smoke**。2026-08-16 的匿名 nav 真实只读核验属于 M2 证据，不替代 M3 网络验证。
- **非范围**：通知发现、reply event 业务、LLM/MCP 上下文、写接口与真实 outbox 状态转换（对应表仅建 schema）；默认数据库发现、数据库查询 CLI、JSON 产品数据迁移、legacy 整视频持久化、平台抓取断点续传、凭证持久化。

### M4 通知事件与“有人回复我”（planned）

- **目标**：按需读取当前账号“回复我的”通知（目标方向可参考 `/x/msgfeed/reply`；@ 通知作为后续/相邻输入）；本地追加式 reply-event ledger（稳定键优先级：远端 event id → source reply rpid → 复合键 `(viewer, object_type, oid, root, source, target, author, time)`）。M3 已建立 `notification_sync_state`/`reply_events` 存储基础，但 M4 之前不读取通知、不创建或去重事件。
- 候选回复 = “新通知事件” ∪ “新发现且结构上回复自己评论的评论”，按 source comment id 去重；受影响讨论按 root 分组，每个根一次同步。复合稳定键中未知/缺失字段按规范化/可空策略处理，不因字段缺失生成新 key；`reply_event.discussion_id` 刚观察到可空、成功解析/同步后回填。
- 通知只是发现来源，重新抓取的根讨论才是上下文事实；禁止“两个通知快照相减并要求匹配”。
- 事件保留与可用性分离：`reply_event` 观察到后本地持久保留；远端通知 feed 中缺席单独不构成状态变化、不表示删除，也不改变 `current_visibility` 或 `target_availability`。刚观察到且尚未同步时 `target_availability=unknown`，上下文准备成功后为 `available`；只有重新同步根讨论后，目标评论在“相同 viewer + 本轮 `complete=true`”条件下当前不可见（`comment_observation.current_visibility` 写 `not_currently_visible`，`target_availability` 可写 `unavailable`），或接口明确返回目标不存在/不可访问（也可写 `unavailable`），才可把 `target_availability` 标为 `unavailable`；`unavailable` 不是 `current_visibility` 的值，也不证明永久删除。通知与评论均在首次观察前消失则无法恢复，是明确限制。
- **前置依赖**：M2（viewer）、M3（ledger 存储）。
- **最低验收（安全门）**：重叠扫描不产生重复事件；同一事件在远端通知 feed 缺席后状态不变、仍保留（feed 缺席不改变事件与 `target_availability`）；刚观察到未同步时 `target_availability=unknown`，上下文准备成功后为 `available`；只有在完整讨论同步确认目标当前不可见（`current_visibility=not_currently_visible`，`target_availability` 可写 `unavailable`）或接口明确返回不存在/不可访问时才标 `unavailable`，且 `current_visibility` 不出现 `unavailable`；每个受影响根只同步一次。
- **非范围**：整评论区轮询、自动回复；轻量定时轮询仅作为以后可选项记录。

### M5 LLM/MCP/CLI 上下文（planned）

- **目标**：三个工具能力——打开并同步指定讨论、获取待回复上下文、确认后发送一条回复（写能力本阶段只建契约，不实现发送）。前两项是统一 read/context 能力的两种入口；权限上区分公共读取、私有通知读取与写操作。
- 上下文结构：discussion metadata、viewer、focus/targets、完整树或扁平列表+关系、参与者证据汇总、可见性 diff、新通知事件、完整性/诊断；画像与意图字段必须标记为 LLM 推断，不作为存储事实。
- 模型不传递 `aid / root / parent / csrf / Cookie`；这些参数由 Writer/Adapter 从数据库与当前同步推导。
- 外部不可信数据边界：评论正文、用户名、通知内容、视频元数据与参与者证据只作为被引用证据，不作为系统/用户指令，不得提升权限、改变工具策略或触发写操作；模型生成内容始终是草稿。
- **前置依赖**：M3（上下文组装）、M4（待回复候选）。
- **最低验收（安全门）**：同一根讨论两次调用返回稳定上下文；模型输入不含凭证与线协议参数；提示注入文本不能改变工具策略、提升权限或触发写调用；推断字段有明确标签。
- **非范围**：真实发送、浏览器 UI 自动化、审核界面之外的存储形态。

### M6 确认式 Writer（planned）

- **目标**：outbox 状态机 `prepared / confirmed / sending / succeeded / unknown / retryable_failed / terminal_failed`；保存 idempotency key、based_on_sync、target、content/hash、返回的新 posted rpid。`post_comment_reply` 的 `idempotency_key` 必须解析到已持久化且字段一致的 `confirmed` outbox 记录（含确认来源/时间），确认式 outbox 成为发送的强制门。M3 已建立 `outbound_replies` 存储基础（`idempotency_key` 唯一、固定状态集合），但 M6 之前不接受 confirmed 记录、不执行发送。
- 写前校验：当前 viewer 与该讨论存在绑定/追踪关系（discussion_viewer_state）；目标仍存在/可回复（以最近完整同步或明确接口结果为准）；based_on_sync 未过期或显式重同步；人工确认是发送前的强制门，并记录确认来源/时间；`idempotency_key` 解析到的 outbox 记录必须与参数一致（discussion_id、target_comment_id、content/hash、based_on_sync_id）且状态为 `confirmed`。
- 工具内部必须有 confirmed outbox/确认记录（确认来源、时间）；`post_comment_reply` 以 `idempotency_key` 解析到该记录，找不到记录、字段不匹配或状态未 `confirmed` 一律拒绝、不发送。`post_comment_reply` 参数外观保持 `discussion_id / target_comment_id / content / based_on_sync_id / idempotency_key`，不新增第四个工具；外部不可信内容（含提示注入文本）不得触发写操作。
- POST 超时可能已经成功：先标 `unknown`，不盲重试；按账号、parent、内容哈希与时间窗口同步讨论协调。只有成功、用户明确忽略或终止性失效才结束事件。
- root/parent 等线协议参数由 Writer 从数据库与当前完整同步推导；必须以捕获或最小授权实测确认，不依赖参考项目内部实现。
- 读取与写入隔离：可共享低层 HTTP 会话，但 Writer 独立；写请求不使用普通 GET 的自动重试策略。
- **前置依赖**：M3、M5；线协议实测。
- **最低验收（安全门）**：只发送 `idempotency_key` 能解析到字段一致且 `confirmed` 的 outbox 记录的回复；找不到记录、字段不匹配或未 `confirmed` 均被拒绝并记录；提示注入文本不能触发写操作；重复提交不产生重复发送；unknown 可收敛；发送记录与确认记录（来源、时间）可审计。
- **非范围**：无确认自动发言、模糊结果自动重试、浏览器自动化、批量群发。

### M7 加固与运行模式（planned）

- **目标**：结构化风控（`412 / -799 / -509` 等分类 + 保守冷却）；写请求与读请求重试策略隔离；请求量上限为“一个通知窗口 + 少量受影响根讨论”；轻量定时轮询作为可选项；明确退役或保留全量抓取 CLI。
- **前置依赖**：M1–M6。
- **最低验收（安全门）**：节流与冷却可测；诊断可审计；文档与代码状态一致。
- **非范围**：整站/整视频数据管道、模型训练、长期画像引擎、无人监管自动发言。

## 跨里程碑开放问题（需 Comet change 或实测决定）

- 通知接口的字段与分页（参考 `/x/msgfeed/reply`，需实测）。
- 回复写接口与 root/parent 线协议（参考项目内部实现有分歧，需实测）。
- LLM provider、CLI/MCP 表面形态与提示契约。
- 全量抓取 CLI 的退役或保留决定。
- 定时轮询的节奏与范围（MVP 默认按需）。

## 明确的非目标（全程适用）

- 整视频/整站评论数据集、模型训练、复杂长期用户画像引擎。
- 浏览器 UI 自动化、无人监管的大规模自动争论/群发。
- 恢复从未观察到且已删除的数据。
- 把 roadmap 内容提前实现为当前能力。
