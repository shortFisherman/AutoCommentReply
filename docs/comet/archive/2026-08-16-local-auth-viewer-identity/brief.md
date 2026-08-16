# Outcome

在现有 M1 定向讨论读取链路上增加本地认证 session 与当前 viewer 身份。匿名读取与登录读取在结构化输出中明确可区分；登录 viewer 使用稳定的 `platform_user_id`（B 站 mid），用户名仅用于展示；每条评论的 `is_self` 只在输出阶段按评论作者身份与 viewer 身份比较派生。任何凭证都不得进入 JSON、诊断、日志、文档或未来模型上下文。

# Scope

- 为 Bilibili 读 Adapter 建立本地认证 session 边界，并把凭证、解析出的 viewer 与 HTTP 客户端生命周期绑定。
- 无本地凭证时建立显式 anonymous viewer，不为识别 viewer 增加网络请求。
- 有本地凭证时，通过 `GET /x/web-interface/nav` 在评论读取前解析并缓存当前 viewer；同一 Adapter 生命周期内与 legacy WBI 密钥读取共享 nav 响应，避免重复请求。
- 在 `FetchResult` 与结构化 JSON 中携带不含凭证的 viewer；定向讨论输出的每条评论与树节点包含输出期派生的三态 `is_self`。
- 保持 discussion identity 与 focus/viewer 无关；保持定向评论读取不调用主评论 `main`、不做 WBI 签名、`root_pages_fetched=0`。
- 更新离线测试、README、当前架构文档与 roadmap 状态，使实现、schema 与文档一致。

# Non-goals

- 通知或“回复我的”读取。
- SQLite、跨运行 viewer/discussion ledger、同步可见性 diff 或任何 M3 数据迁移。
- 评论写接口、登录 UI、扫码登录、浏览器自动化、自动刷新 Cookie 或系统 keyring 集成。
- LLM/MCP 上下文组装；本 change 只建立其未来可消费的无凭证身份边界。
- 改造或退役 legacy 整视频诊断模式。

# Acceptance examples

- A1：不提供凭证时，定向输出包含 anonymous viewer（`authenticated=false`、`platform_user_id=null`、`username=null`），所有评论与树节点的 `is_self=null`；不会为了 viewer 识别请求 nav。
- A2：提供有效登录凭证时，Adapter 在评论读取前从一次 nav 响应解析出 `authenticated=true`、正整数 `platform_user_id` 与仅展示的 username；同一 Adapter 生命周期重复使用缓存，legacy WBI 取钥匙与 viewer 识别不重复请求 nav。
- A3：登录 viewer 下，作者身份已知且等于 `viewer.platform_user_id` 的评论输出 `is_self=true`，已知且不等的输出 `false`；viewer 匿名或作者身份未知时输出 `null`。`is_self` 不存入 `Comment` 事实模型。
- A4：提供了凭证但 nav 返回未登录、无有效正整数 mid、缺少必要登录字段或响应结构无效时，读取 fail closed：抛出类型化认证/解析错误，CLI exit 1 且不输出 discussion JSON，不得静默降级为匿名。
- A5：同一评论链接在匿名与任一登录 viewer 下得到相同的 discussion identity 与同步范围；定向路径仍不调用主评论 `main`、不做 WBI 签名，且 `root_pages_fetched=0`。M2 仅允许为登录 session 增加每个 Adapter 生命周期至多一次、可与 legacy WBI 共用的 nav 身份请求。
- A6：含明显唯一标记的测试凭证不出现在 stdout、写盘 JSON、stderr（含 verbose）、异常文本、diagnostics/details、对象 repr、README/架构文档或已跟踪文件；不新增接收命令行明文 Cookie 的参数。
- A7：legacy schema 1.0 的现有输出契约与行为保持不变；定向输出采用经用户确认的 M2 schema/作者字段兼容策略，旧 M1 fail-closed、complete、diagnostics 与 exit 0/1/2 语义不回归。
- A8：`uv run ruff format --check .`、`uv run ruff check .`、`uv run pytest` 与覆盖率检查通过；脱敏 fixture 覆盖 anonymous、有效登录、失效登录、viewer 解析、`is_self` 三态、nav 请求预算和秘密泄漏路径。

# Constraints and invariants

- B 站网页私有接口字段只存在于 Adapter 边界；登录字段以脱敏捕获或最小授权实测为依据。2026-08-16 已匿名只读核验 nav 返回 `code=-101`、`isLogin=false`、`mid=null`、`uname=null` 且仍含 WBI 数据；登录响应仍需脱敏 fixture/最小授权验证。
- `platform_user_id` 是 viewer 稳定身份，username 仅展示且不得参与身份比较。
- 评论作者的稳定身份是 `author_id`；`is_self` 是 viewer-relative 派生值，不进入讨论身份、不成为持久事实。
- 凭证只能存在于本机认证输入、进程内 session 与发往允许 Bilibili 主机的 HTTP Cookie header；不得放入 URL、argv、模型输入或任何可提交产物。
- 提供凭证表示用户要求登录视角；身份不可确认时必须失败，不得回退匿名造成错误可见性语义。
- 当前 change 使用单一 Native change：认证解析、viewer 模型与输出三态共享同一核心数据流，无法形成有真实独立验收价值的 children。

# Decisions

- 使用现有 Bilibili `nav` 端点识别登录 viewer；无凭证时不请求 nav，有凭证时在评论读取前解析并在 Adapter 生命周期内缓存。
- anonymous viewer 使用显式对象和 JSON `null` 表达未知身份；不会把 anonymous 的 `is_self` 默认为 `false`。
- 失效或无法确认的已提供凭证 fail closed，CLI exit 1 且不产生 JSON。
- `is_self` 仅在输出时派生；discussion identity、focus 与同步范围不依赖 viewer。
- legacy 整视频诊断 schema 1.0 保持不变；M2 的新 viewer-aware schema 只作用于定向讨论输出。
- 定向讨论输出升级为 schema 1.2；评论作者只输出规范字段 `author_id`，不保留 `user_id` 兼容别名。schema 1.1 消费者需显式迁移，legacy schema 1.0 的 `user_id` 保持不变。
- 本地认证输入沿用显式 `--cookie-file` / `BILIBILI_COOKIE`；程序在进程内建立包含凭证边界与已解析 viewer 的 session，不新增 `auth.json`、`--auth-file`、默认凭证路径或跨运行认证状态。

# Open questions

无。

# Verification expectations

- Builder 运行格式、lint、全量离线测试与覆盖率检查，并提供脱敏的 nav 登录/匿名 fixture；测试不得访问真实账号。
- Runtime 检查命令不得包含 Cookie 文本或本地凭证文件内容，日志预览必须可安全进入 verification 报告。
- 新的只读 Verifier 逐项检查 A1–A8、源代码秘密边界与文档一致性；不得读取本地真实 Cookie。
- 如用户另行授权并提供工作区之外的本地凭证路径，可追加一次最小只读登录 smoke；它用于确认私有登录字段，不作为自动测试依赖，也不得在命令、日志或报告中暴露凭证。
