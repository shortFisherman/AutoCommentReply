# SQLite 持久化同步语义

## 能力目标

系统可以把用户主动选择的定向讨论同步到显式指定的本地 SQLite 数据库。数据库保存规范化讨论事实、viewer-relative 观察、完整同步 baseline 和追加式 sync run ledger，使同一讨论能够跨进程、跨小时重复同步，并以保守语义回答“本轮首次观察到什么”和“相对上一完整同步，什么当前不可见”。

本能力只陈述观察事实。`not_currently_visible` 不证明永久删除、屏蔽原因或全局不可用；只有同一 viewer 的最终 `complete=true` 同步可以推进当前可见性 baseline。不完整同步可以吸收新观察，但不能依据缺失项作负面推断。

## CLI 启用与兼容

- 现有 `auto-comment-reply <reference>` 命令新增可选 `--database PATH`。只有提供该参数且 reference 解析为用户选中的定向 discussion 时才启用 SQLite 持久化。
- 未提供 `--database` 时，网络请求、定向 schema 1.2、legacy schema 1.0、stdout/文件输出、`--force` 覆盖保护和 exit 0/1/2 语义保持现状。
- `--database` 不建立默认路径、不读取环境变量数据库路径、不自动发现数据库，也不在应用数据目录产生隐藏状态。
- legacy 整视频诊断 reference 与 `--database` 组合必须在持久化前被拒绝，CLI exit 1 且不产生 JSON；M3 不把整视频读取变成产品数据管道。
- 第一次成功提交某个 `(viewer, discussion)` 的数据库事务时，系统自动建立其 bound/tracked state，与该 run 的 `complete` 无关；M3 不增加单独的 track/untrack 命令。
- 定向 JSON 输出保持 schema 1.2，不加入数据库路径、内部主键、sync_run id、baseline 或 diff 字段。SQLite 状态由 Python storage/query API 提供给后续能力；M3 不增加最终用户数据库查询 CLI。

## 存储边界与 schema 生命周期

- SQLite 位于 Adapter 之外。Adapter 继续只负责引用解析、viewer 解析、平台请求、分页、规范化 comment facts 与 diagnostics；不得接收连接、执行 SQL 或持久化平台抓取游标。
- storage 层负责连接、schema version、迁移、约束、索引、事务和查询；sync 层负责把一次已经完成结构校验的定向 `FetchResult` 应用为一个原子 sync run。
- 使用 Python 标准库 `sqlite3`，不引入 ORM。数据库必须有显式 schema version，并只执行已知、单调、事务化的迁移；遇到比当前程序更新或无法识别的 schema 必须 fail closed，不得猜测、降级或破坏数据。
- 平台 ID 必须无截断、无浮点转换和无精度损失。SQLite 物理列型可由实现决定，但 API 对外保持规范化的完整 ID 值。
- 所有持久化时间使用 UTC，并以稳定、可排序的格式或整数表示；跨时区查询不得改变顺序或身份。
- 数据库包含 viewer mid、展示用户名和评论正文，属于本地私有产品数据。Cookie、CSRF、request headers、认证文件内容和其他凭证永不进入数据库、SQL 参数日志、异常或 diagnostics。

## Viewer

- viewer 的稳定已认证身份是 `(platform, platform_user_id)`；Bilibili `platform_user_id` 是 nav 确认的正整数 mid。username 仅为可更新的展示字段，不参与身份、授权、discussion identity 或 baseline key。
- 每个数据库中，每个平台恰有一个稳定 anonymous viewer。其 `authenticated=false`、`platform_user_id=null`、`username=null`；重复 anonymous 同步必须解析到同一内部 viewer id，不能受 SQLite `NULL != NULL` 的唯一性行为影响而生成重复实体。
- anonymous viewer 与任意 authenticated viewer 完全隔离。同一 discussion 在不同 viewer 下有不同的 discussion_viewer_state、ever-seen、last-complete baseline、comment observations 与 sync runs。
- M2 的认证 session 仍是进程内一次性 session；持久化 viewer 身份不持久化 Cookie，也不能在后续进程中代替 nav 身份确认。

## Discussion 与 comment facts

- discussion 自然身份是 `(platform, object_type, oid, root_comment_id)`，与 viewer、username、focus_comment_id 和入口 URL 无关。`oid` 是平台对象 ID 的规范槽位；当前 Bilibili video discussion 中它与已解析 `aid` 同值。bvid、focus 和其他展示/定位信息可以作为可更新元数据保存，但不能改变自然身份。
- comments 属于 discussion，并以 `(discussion_id, platform_comment_id)` 唯一。重复页面、重复同步或不同 viewer 观察同一 comment 不能复制 comment fact 实体。
- comment facts 至少保存规范化关系、作者、内容和平台时间：platform comment id、root id、parent id、author id、author display name、content、created_at，以及当前 Comment 事实模型中与展示相关且不属于 viewer-relative 观察的字段。
- comments 不保存 `is_self`、单一全局 visibility、树节点、conversation chain、Cookie/CSRF/header、请求 URL/query、分页游标或其他平台线协议字段。`is_self` 继续由 `comment.author_id == viewer.platform_user_id` 在输出或查询组装阶段派生；anonymous 或作者未知时为 null。
- 本轮观察到的全部规范化 comment IDs 都属于 `observed_ids`，包括结构校验后仍保留的孤儿 facts；树与父链问题通过 diagnostics 表达，不通过静默丢弃 comment 改写 observed 集合。
- 未知占位值不得覆盖已保存的更完整事实；后续更完整观察可以回填未知值。关系冲突不得被静默覆盖，必须产生可审计错误并使最终 complete 判定为 false。

## Viewer-relative state 与 observation

- `discussion_viewer_state` 唯一键为 `(discussion_id, viewer_id)`，至少保存 bound/tracked 状态、ever-seen 语义、最近一次完整同步引用和 last-complete visible baseline。
- `comment_observation` 唯一键为 `(discussion_id, viewer_id, comment_id)`，至少保存 `first_seen_at`、`last_seen_at` 和 `current_visibility`。
- `current_visibility` 可以为空，表示该 comment 尚未进入任何完整 baseline，因而没有足够证据给出当前可见性结论；这包括首次只在 `complete=false` run 中观察到的 comment。非空合法值只有 `visible` 与 `not_currently_visible`。它只描述该 viewer 对该 discussion 的最近完整 baseline；不得出现 `unavailable`、`deleted` 或其他永久性结论。
- ever-seen 在 `(discussion_id, viewer_id)` 作用域内单调增长。实现可以由 observations 推导或规范化存储，但查询 API 必须稳定返回相同语义，并由约束/事务保证不会回退。
- first_seen 一经提交不得被后续同步改写；观察到 comment 时更新 last_seen。再次观察到此前 `not_currently_visible` 的 comment 时，它恢复为 `visible`，但 first_seen 保持不变。

## Sync run ledger

- 每次启用持久化的定向同步追加一个 sync run。run 至少关联 viewer 与 discussion，并记录开始/完成时间、最终 observed_ids、最终 complete、diagnostics，以及足以查询本轮 newly-observed 和 visibility diff 的数据。
- persisted `complete` 必须与同一运行最终 schema 1.2 输出文档的 `complete` 完全一致，即经过 Adapter、分页、父链/建树等结构校验和输出期 error 复核后的最终值；不能在输出阶段还可能新增 error 时提前把 Adapter 的中间状态作为完整 baseline 提交。
- diagnostics 使用现有脱敏结构语义持久化。它们不得包含 Cookie、headers、认证文件内容、未脱敏服务端 payload 或数据库绝对路径中的敏感内容。
- sync runs 是追加式审计事实。相同快照重复运行可以追加新的 run，但不得复制 viewer/discussion/comment/observation 实体，也不得产生虚假的 newly-observed 或 visibility diff。

## 完整同步算法

对一个 `(viewer_id, discussion_id)`，每轮在同一事务中执行以下语义：

1. 读取事务开始时最近一次完整同步的 `last_complete_visible_ids`，记为 `previous_visible_ids`；没有完整 baseline 时为空集。
2. 对最终 FetchResult 中所有规范化 comments 形成 `observed_ids`，并读取提交前的 `ever_seen_before`。
3. 计算 `newly_observed = observed_ids - ever_seen_before`，把 observed_ids 并入 ever-seen；该步骤不依赖 complete。
4. upsert discussion/comment facts，并对本轮观察到的 comments 保留 first_seen、更新 last_seen。
5. 如果最终 `complete=true`：
   - `current_visible_ids = observed_ids`；
   - `not_currently_visible_ids = previous_visible_ids - current_visible_ids`；
   - observed comments 的 current_visibility 写为 `visible`；差集中的 observations 只推进为 `not_currently_visible`；
   - last-complete baseline 原子替换为 current_visible_ids，并引用本轮 run。
6. 如果最终 `complete=false`：
   - 不替换 last-complete baseline；
   - 不计算或写入缺失、删除、unavailable 或 not-currently-visible 差集；
   - 只提交 observed facts、ever-seen/first_seen/last_seen、新观察结果、run 与 diagnostics；既有 current_visibility 继续描述上一完整 baseline，首次仅在本轮观察到的 comment 保持 current_visibility 为空。

完整且 observed_ids 为空是合法的 sync/storage 层集合语义：新 baseline 为空，上一完整 baseline 的全部 IDs 进入 `not_currently_visible`；这仍不证明永久删除。当前真实定向 Adapter 的有效完整结果通常至少含根评论，但该底层不变量仍必须可直接测试。首次完整同步以空 previous baseline 计算，所有 observed IDs 都是 newly observed 且 visible。

## 事务、并发与崩溃恢复

- 一个 sync run 的原子提交单元包含 viewer/discussion state、comment facts、observations、sync run、ever-seen 和（仅 complete 时）visibility diff/baseline。任何约束、schema、锁定、I/O 或提交错误都必须回滚整个单元。
- baseline 的 read-modify-write 必须防止两个并发进程以同一陈旧 baseline 覆盖彼此。实现可以串行化 writer 或使用条件更新，但最终提交顺序必须形成确定的完整 baseline 历史。
- 进程在事务任意位置崩溃后，重新打开数据库只能看到上一个完整提交或本轮完整提交，不得看到半份 sync run、半份 baseline 或缺少对应 run 的 observations。
- 数据库可以采用 WAL、busy timeout 与完整性 pragma；具体参数属于实现选择。超出允许等待仍无法取得锁时按持久化失败处理，不得跳过 ledger 后声称同步成功。
- DB commit 是持久化同步的权威完成点。JSON stdout/导出是现有的次级展示：程序必须先构建并校验文档，再提交数据库，提交成功后才发布 JSON。SQLite 与 stdout/文件系统无法跨介质组成共同原子事务；若数据库已成功提交而后续 JSON 发布发生罕见 I/O 失败，数据库提交保持有效，CLI 仍按现有输出错误返回 exit 1，重试必须依靠同步幂等性安全收敛。

## 查询 API

- M3 提供稳定的 Python storage/query API，至少可以：
  - 按 viewer identity 与 discussion identity 查找实体和 tracked state；
  - 列出/读取某 viewer 的 discussions，并以稳定顺序跨小时查询；
  - 读取某 viewer/discussion 的 ever-seen、last complete baseline、current observations；
  - 读取 sync run 历史及单轮 observed/newly-observed/visibility diff/complete/diagnostics；
  - 读取规范化 comments 供后续树、上下文与 `is_self` 派生使用。
- 查询必须显式接受 viewer scope，不得把不同 mid 或 anonymous 的 observations 合并。稳定排序至少使用持久时间和内部稳定键打破并列。
- API 不返回 Cookie、CSRF、headers、数据库连接对象或平台写参数；数据库内部主键不能替代平台稳定身份进入模型上下文。
- M3 不增加最终用户查询 CLI，也不改变定向 JSON schema；M4/M5 在此 API 上定义其事件和上下文表面。

## 后续里程碑的 schema 基础

- schema 包含可迁移的 `notification_sync_state`、`reply_events` 与 `outbound_replies/outbox` 基础实体，以避免 M4/M6 重新定义 ledger 身份；M3 不读取通知、不创建真实 reply events、不发送回复，也不驱动 outbox 状态机。
- `reply_event.target_availability` 的合法值是 `unknown / available / unavailable`，并与 event status 使用独立列和独立约束。`unavailable` 永远不是 `comment_observation.current_visibility` 的值。
- outbound/outbox 基础必须能在后续保存 idempotency key、target、content/hash、based-on sync 与状态，但 M3 不接受 confirmed 记录、不执行发送，也不声称已经实现 M6 的完整状态机。

## 错误语义

- 提供 `--database` 后，数据库路径无效、打开失败、schema 不兼容、迁移失败、锁超时、约束冲突、事务失败或 commit 失败均为持久化致命错误：事务回滚，CLI exit 1，stdout 与目标输出文件均不产生本轮 JSON。
- 持久化错误使用固定、脱敏、可操作的错误类别；不得回显 SQL 中的私有评论内容、凭证、headers 或完整服务端 payload。
- 远端/结构同步本身 `complete=false` 但数据库事务成功时，必须按不完整同步规则持久化 run，随后继续输出现有 schema 1.2 JSON 并返回现有 exit 2。
- 同一运行既是远端/结构 `complete=false` 又发生 SQLite 致命失败时，持久化 fail-closed 优先：事务回滚、exit 1 且无 JSON；不能因原本可返回 exit 2 而掩盖 ledger 未提交。
- 远端读取前的输入、认证、Cookie 文件或输出目标预检错误继续遵循现有 exit 1、无 JSON 行为。

## 明确不包含

本能力不实现通知抓取、notification feed 缺席推断、reply event 发现与去重、LLM/MCP 上下文、意图或画像持久化、Writer、真实 outbox 状态转换、确认或发送、默认数据库发现、JSON 产品数据迁移、legacy 整视频持久化、平台抓取断点续传或凭证持久化。
