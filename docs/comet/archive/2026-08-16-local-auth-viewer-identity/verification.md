---
generated_from_state_version: 10
---

# Verification

## Current result

- Result: **Passed**
- Assurance: **skill-coordinated**
- Goal cycle: 1
- Iteration: 2
- Verifier attempt: 1
- Completed: 2026-08-16T09:21:27.297Z
- Summary: A18 已真正修复：Viewer 改为 eq=False 并自定义基于 (platform, authenticated, platform_user_id) 的 __eq__/__hash__，username 仅保留在 to_dict 展示，不参与相等性、哈希、discussion identity、同步范围、is_self 派生或任何授权判断，并新增三组回归测试。静态语义审阅复核 A1-A35：认证 fail-closed、nav 缓存与 legacy WBI 共享一次响应、schema 1.2 的 viewer/author_id/三态 is_self、legacy 1.0 不回归、秘密不泄漏、无越界功能均成立。Runtime 三项检查均 passed/exit 0（ruff format --check、ruff check、pytest -q --cov=auto_comment_reply --cov-report=term-missing；主流程观测 168 passed、总覆盖率 90%）。全部 35 项验收 passed，verdict=pass。

## Acceptance

| ID | Result | Source | Criterion | Reason |
| --- | --- | --- | --- | --- |
| A1 | passed | brief.md | A1：不提供凭证时，定向输出包含 anonymous viewer（`authenticated=false`、`platform_user_id=null`、`username=null`），所有评论与树节点的 `is_self=null`；不会为了 viewer 识别请求 nav。 | adapter._resolve_viewer 在无凭证分支直接返回 ANONYMOUS_VIEWER（authenticated=false、platform_user_id=null、username=null），不发任何网络请求；output.derive_is_self 在 viewer 未认证时返回 None。tests/test_viewer.py::test_anonymous_discussion_viewer_is_explicit_and_nav_is_never_requested 与 tests/test_cli.py::test_cli_anonymous_run_emits_anonymous_viewer_without_nav 断言定向路径无 nav 且全部 is_self=null。 |
| A2 | passed | brief.md | A2：提供有效登录凭证时，Adapter 在评论读取前从一次 nav 响应解析出 `authenticated=true`、正整数 `platform_user_id` 与仅展示的 username；同一 Adapter 生命周期重复使用缓存，legacy WBI 取钥匙与 viewer 识别不重复请求 nav。 | fetch_discussion 在 resolve_video 前调用 _resolve_viewer，用一次 GET /x/web-interface/nav 解析 isLogin=true 与正整数 mid（_strict_positive_int），uname 仅展示；_nav_payload 与 _viewer 在 Adapter 生命周期内缓存。test_authenticated_discussion_resolves_nav_before_comment_read_and_caches_it 断言 nav 计数为 1 且先于评论读取；test_authenticated_adapter_shares_one_nav_between_legacy_wbi_and_discussion 断言 legacy WBI + 两次 discussion 共 1 次 nav。 |
| A3 | passed | brief.md | A3：登录 viewer 下，作者身份已知且等于 `viewer.platform_user_id` 的评论输出 `is_self=true`，已知且不等的输出 `false`；viewer 匿名或作者身份未知时输出 `null`。`is_self` 不存入 `Comment` 事实模型。 | output.derive_is_self 仅比较 comment.user_id（事实模型作者身份）与 viewer.platform_user_id，Comment 数据类无 is_self 字段。test_authenticated_discussion_derives_is_self_tristate_in_output 与 tests/test_output.py 断言 true/false/null 三态且事实模型不含 is_self。 |
| A4 | passed | brief.md | A4：提供了凭证但 nav 返回未登录、无有效正整数 mid、缺少必要登录字段或响应结构无效时，读取 fail closed：抛出类型化认证/解析错误，CLI exit 1 且不输出 discussion JSON，不得静默降级为匿名。 | _parse_viewer 要求 isLogin 严格为 True 且 mid 可解析为正整数，否则抛类型化 AuthenticationError/ResponseParseError；_resolve_viewer 无匿名回退分支；cli.main 捕获后 exit 1 且不生成 JSON。test_authenticated_nav_invalid_fails_closed_before_comment_read（12 种非法形态，reply_calls==0）与 test_cli_auth_failure_exits_1_without_json_on_stdout_or_disk 覆盖。 |
| A5 | passed | brief.md | A5：同一评论链接在匿名与任一登录 viewer 下得到相同的 discussion identity 与同步范围；定向路径仍不调用主评论 `main`、不做 WBI 签名，且 `root_pages_fetched=0`。M2 仅允许为登录 session 增加每个 Adapter 生命周期至多一次、可与 legacy WBI 共用的 nav 身份请求。 | discussion identity 仅由 (platform, object_type, oid, root_comment_id) 构成（reference.py 未变）；fetch_discussion 只调用 view 与 /x/v2/reply/reply，不进入 _get_mixin_key/sign_wbi_params/main 路径，stats.root_pages_fetched 保持 0。test_discussion_identity_scope_and_root_pages_are_viewer_independent 与 test_username_never_affects_is_self_or_discussion_identity 断言匿名/多登录 viewer 下 identity 与 stats 一致。 |
| A6 | passed | brief.md | A6：含明显唯一标记的测试凭证不出现在 stdout、写盘 JSON、stderr（含 verbose）、异常文本、diagnostics/details、对象 repr、README/架构文档或已跟踪文件；不新增接收命令行明文 Cookie 的参数。 | 唯一标记假凭证（SESSDATA=opdeepseekflash_m2_fake_secret_*）经 grep 确认不出现在 README/架构/roadmap/项目文档与源码输出路径；tests 对 stdout/文件/stderr/repr/异常/details 递归断言不含 secret；cli.py 未新增任何明文 Cookie 参数。该假 secret 仅存在于 A34 授权的脱敏 fixture tests/_helpers.py。 |
| A7 | passed | brief.md | A7：legacy schema 1.0 的现有输出契约与行为保持不变；定向输出采用经用户确认的 M2 schema/作者字段兼容策略，旧 M1 fail-closed、complete、diagnostics 与 exit 0/1/2 语义不回归。 | build_output_document 在 discussion 为 None 时沿用 schema 1.0、Comment.to_dict（含 user_id）且不含 viewer/author_id/is_self；legacy 既有测试在 Runtime pytest 全量通过；test_legacy_schema_1_0_fields_and_shape_are_unchanged 与 test_cli_incomplete_result_is_written_and_returns_two 覆盖旧契约与 exit 0/2。 |
| A8 | passed | brief.md | A8：`uv run ruff format --check .`、`uv run ruff check .`、`uv run pytest` 与覆盖率检查通过；脱敏 fixture 覆盖 anonymous、有效登录、失效登录、viewer 解析、`is_self` 三态、nav 请求预算和秘密泄漏路径。 | Runtime 三项检查均 passed/exit 0：ruff format --check（107ms）、ruff check（104ms）、pytest -q --cov=auto_comment_reply --cov-report=term-missing（1609ms）；主流程观测 168 passed、总覆盖率 90%。脱敏 fixture 覆盖 anonymous、有效/失效登录、viewer 解析、is_self 三态、nav 预算与 secret 泄漏路径。 |
| A9 | passed | specs/local-auth-viewer-identity/spec.md | 系统在一次只读讨论同步中必须明确知道它是以 anonymous 视角还是某个已认证 Bilibili viewer 视角运行。viewer 的稳定身份是 `platform_user_id`（B 站 mid）；username 仅用于展示。认证材料保留在本机 session 边界内，永不成为输出或模型可见数据。 | Viewer 模型以 platform/authenticated/platform_user_id/username 表达视角，platform_user_id 为稳定身份；FetchResult 携带 viewer、定向输出顶层含 viewer；凭证只存于 Adapter 的 Cookie header 与进程内字段，不进入输出或模型可见数据。 |
| A10 | passed | specs/local-auth-viewer-identity/spec.md | 没有认证输入时，系统创建 anonymous session；`authenticated=false`，`platform_user_id=null`，`username=null`。 | 无凭证时 _authenticated_session=False，_resolve_viewer 返回模块级 ANONYMOUS_VIEWER（authenticated=false、platform_user_id=null、username=null）；test_viewer.py 与 test_cli.py 匿名用例断言该形状。 |
| A11 | passed | specs/local-auth-viewer-identity/spec.md | 有认证输入时，系统创建 authenticated session，将凭证仅注入允许 Bilibili 主机的 HTTP Cookie header，并在读取评论前确认当前 viewer。 | 有凭证时构造 httpx.Client 即把 Cookie 注入 header；所有请求仅发往 api.bilibili.com 与 b23.tv（_ALLOWED_BILIBILI_HOSTS），follow_redirects=False 且跳转目标经主机白名单校验；fetch_discussion 在评论端点前解析 viewer。 |
| A12 | passed | specs/local-auth-viewer-identity/spec.md | viewer 通过 `GET https://api.bilibili.com/x/web-interface/nav` 解析。登录响应必须明确表示已登录，并提供可解析为正整数的 mid；username 可空且仅展示。 | _parse_viewer 消费 GET /x/web-interface/nav 的 data：要求 isLogin 为 True、mid 经 _strict_positive_int（拒绝 bool/float/非法字符串），uname 允许 None 或字符串；test_authenticated_nav_accepts_numeric_string_mid_and_nullable_username 覆盖数字字符串 mid 与空 username。 |
| A13 | passed | specs/local-auth-viewer-identity/spec.md | 同一 Adapter 生命周期内 nav 响应缓存并复用；legacy WBI 密钥读取如同时需要 nav，必须共享该响应，不能重复身份请求。 | _resolve_viewer 与 _get_mixin_key 共用 _nav_payload 缓存：viewer 先解析后 WBI 复用同一响应，不重复身份请求；test_authenticated_adapter_shares_one_nav_between_legacy_wbi_and_discussion 断言 legacy fetch + 两次 discussion 的 nav 总次数为 1。 |
| A14 | passed | specs/local-auth-viewer-identity/spec.md | 提供了凭证但 nav 表示未登录、mid 缺失/非法或结构无效时必须 fail closed，不得按 anonymous 继续。 | 认证分支无任何回退到 ANONYMOUS_VIEWER 的代码路径；nav 未登录（code=-101/isLogin 非 True）、mid 缺失/非法、结构无效均抛类型化错误；参数化测试覆盖 12 种形态并断言评论读取不开始。 |
| A15 | passed | specs/local-auth-viewer-identity/spec.md | 认证输入只沿用现有 `--cookie-file` 与 `BILIBILI_COOKIE` 环境变量；`--cookie-file` 保持优先。系统不新增 `auth.json`、`--auth-file`、默认凭证路径或跨运行认证状态，也不得新增 argv 明文 Cookie。 | 认证输入仅沿用 --cookie-file（优先）与 BILIBILI_COOKIE；cli.py 参数表未改、无明文 Cookie 参数；grep 无 auth.json/--auth-file/默认凭证路径/跨运行认证状态；test_cookie_file_takes_precedence_and_is_not_echoed 与 test_cli_env_cookie_is_used_when_no_cookie_file 覆盖优先级。 |
| A16 | passed | specs/local-auth-viewer-identity/spec.md | Cookie 输入与解析出的 viewer 在进程内组成同一 session 边界；session 生命周期不超过 Adapter 生命周期，关闭 Adapter 时同步结束。 | cookie 与解析出的 viewer 均为 Adapter 实例内的进程内状态（client header、_viewer、_nav_payload），无任何持久化；close()/__exit__ 关闭自有 client 结束 session，生命周期不超过 Adapter。 |
| A17 | passed | specs/local-auth-viewer-identity/spec.md | viewer 输出是无凭证事实对象： | Viewer 仅含 platform/authenticated/platform_user_id/username，无 Cookie 字段；to_dict 输出即无凭证事实对象；test_viewer_model_exposes_credential_free_fact_shape 断言 shape 且 UNIQUE_SECRET 不在 repr 中。 |
| A18 | passed | specs/local-auth-viewer-identity/spec.md | anonymous viewer 的 `platform_user_id` 与 `username` 均为 JSON `null`。username 不参与相等性、discussion identity、同步范围或任何授权判断。 | 本轮修复项：Viewer 声明 eq=False 并自定义 __eq__/__hash__，仅基于 identity=(platform, authenticated, platform_user_id)，username 不参与相等性/哈希但保留在 to_dict 展示；DiscussionReference.identity 不含 viewer/username；derive_is_self 只用 platform_user_id。回归测试 test_viewer_equality_and_hash_ignore_username、test_viewer_equality_requires_same_platform_authenticated_and_mid、test_viewer_equality_with_non_viewer_returns_not_implemented 与 test_username_never_affects_is_self_or_discussion_identity 均在 Runtime pytest 中通过。 |
| A19 | passed | specs/local-auth-viewer-identity/spec.md | 评论作者稳定身份使用 `author_id`。M2 定向 schema 只输出 `author_id`，不输出 `user_id` 兼容别名；legacy schema 1.0 继续使用原有 `user_id`。 | 定向输出经 _discussion_comment_document 只发 author_id（取自 Comment.user_id 事实字段），不发 user_id 别名；legacy 分支继续用 Comment.to_dict 输出 user_id；test_output.py 断言 discussion 文档全树无 user_id、legacy 文档含 user_id。 |
| A20 | passed | specs/local-auth-viewer-identity/spec.md | `is_self` 不存入平台中立 Comment 事实模型；输出层对平面 comments 与嵌套 trees 统一派生。 | Comment 数据类没有 is_self 字段；build_output_document 在输出层对平面 comments 与 tree.to_dict(comment_serializer=...) 统一使用 derive_is_self；test_is_self_is_output_derived_and_not_stored_on_comment_fact 与树节点断言覆盖。 |
| A21 | passed | specs/local-auth-viewer-identity/spec.md | viewer 已认证且作者身份已知时：`author_id == viewer.platform_user_id` 为 `true`，否则为 `false`。 | derive_is_self 在 viewer 已认证且 comment.user_id>0 时返回 comment.user_id == viewer.platform_user_id；测试断言 author_id==viewer 的评论 is_self=true、不等为 false（test_discussion_schema_1_2_emits_viewer_author_id_and_is_self_tristate 等）。 |
| A22 | passed | specs/local-auth-viewer-identity/spec.md | viewer anonymous 或作者身份未知时，`is_self=null`；不得用 `false` 伪装未知。 | viewer 未认证或 comment.user_id<=0（作者未知占位）时 derive_is_self 返回 None；test_authenticated_discussion_derives_is_self_tristate_in_output（include_mid=False）与 test_anonymous_discussion_viewer_and_all_is_self_are_null 断言 null 而非 false。 |
| A23 | passed | specs/local-auth-viewer-identity/spec.md | 定向讨论输出从 schema 1.1 升级为 schema 1.2，新增顶层 `viewer`，并将评论及树节点的作者字段从 `user_id` 替换为 `author_id`；不双发兼容别名。依赖 schema 1.1 的消费者必须按版本显式迁移。 | SCHEMA_VERSION_DISCUSSION 升为 1.2；定向输出新增顶层 viewer，comments/trees 作者字段替换为 author_id 且不双发 user_id（assert_key_absent 递归断言）；文档明确 schema 1.1 消费者需按版本迁移。 |
| A24 | passed | specs/local-auth-viewer-identity/spec.md | legacy 整视频诊断输出继续使用 schema 1.0，字段与现有行为不变。 | legacy 整视频输出仍为 schema 1.0，video/stats/comments/trees/diagnostics 字段与序列化路径未变（无 viewer/author_id/is_self）；test_legacy_schema_1_0_fields_and_shape_are_unchanged 与既有 legacy 测试在 Runtime 全量运行中通过。 |
| A25 | passed | specs/local-auth-viewer-identity/spec.md | discussion identity 继续是 `(platform, object_type, oid, root_comment_id)`，不包含 viewer 或 focus。 | DiscussionReference.identity 保持 (platform, object_type, oid, root_comment_id)，不含 viewer 或 focus；test_discussion_identity_scope_and_root_pages_are_viewer_independent 断言不同 viewer 下 identity 相同。 |
| A26 | passed | specs/local-auth-viewer-identity/spec.md | viewer 只改变 viewer-relative `is_self` 和平台可见范围；同一输入的目标根、focus 规则、建树规则与请求分页范围不变。 | viewer 只影响输出期 is_self；目标根、focus 规则、建树与分页逻辑（fetch_discussion/build_comment_forest）不读取 viewer；test_output.py 断言多 viewer 下 discussion、root_pages_fetched、reply_pages_fetched、total_comments_fetched 一致。 |
| A27 | passed | specs/local-auth-viewer-identity/spec.md | anonymous 定向同步不为身份识别请求 nav。 | 匿名定向同步的 _resolve_viewer 分支零网络请求；test_anonymous_discussion_viewer_is_explicit_and_nav_is_never_requested 与 test_cli_anonymous_run_emits_anonymous_viewer_without_nav 断言请求路径无 /x/web-interface/nav。 |
| A28 | passed | specs/local-auth-viewer-identity/spec.md | authenticated 定向同步每个 Adapter 生命周期至多增加一次 nav 身份请求；评论读取本身仍不调用主评论 `main`、不做 WBI 签名，`root_pages_fetched=0`。 | 认证定向每个 Adapter 生命周期仅一次 nav 身份请求（_viewer 缓存），评论读取只用 /x/v2/reply/reply、不调用 main/WBI，root_pages_fetched=0；test_authenticated_discussion_resolves_nav_before_comment_read_and_caches_it 断言 nav 计数 1、无 /x/v2/reply/wbi/main。 |
| A29 | passed | specs/local-auth-viewer-identity/spec.md | 认证身份无法确认发生在评论读取前，作为读取前致命错误返回 CLI exit 1，不产生 discussion JSON。 | 身份确认失败发生在 fetch_discussion 的评论请求之前（_resolve_viewer 先于 resolve_video 与 reply 请求），CLI 捕获 BilibiliError 返回 1 且不写 stdout/文件；test_cli_auth_failure_exits_1_without_json_on_stdout_or_disk 断言 exit 1、stdout 空、输出文件不存在。 |
| A30 | passed | specs/local-auth-viewer-identity/spec.md | 评论读取期间现有 complete、diagnostics 与 exit 0/2 语义保持不变。 | 评论读取阶段的错误仍转为 diagnostics 并置 complete=false，CLI 在 JSON 已生成后返回 2；test_cli_incomplete_result_is_written_and_returns_two 与既有分页/树诊断测试在 Runtime 运行中通过，无回归。 |
| A31 | passed | specs/local-auth-viewer-identity/spec.md | 凭证不得出现在 stdout、输出文件、stderr/verbose 日志、异常消息、diagnostics/details、对象 repr、文档、fixture、Runtime handoff、Verifier 报告或模型上下文。 | 全仓库 grep 未发现任何真实凭证；错误文本为固定脱敏文案；Runtime handoff/comet-state.yaml 与文档不含 Cookie 内容。唯一标记假凭证只存在于 A34 要求的脱敏 fixture tests/_helpers.py，测试断言其不出现在任何可观察输出。 |
| A32 | passed | specs/local-auth-viewer-identity/spec.md | 不允许将 Cookie 放入 URL/query、命令行明文参数或可提交文件。 | Cookie 只经 --cookie-file 文件路径或 BILIBILI_COOKIE 环境变量进入，不拼接 URL/query、无命令行明文参数；test_cli_authenticated_cookie_file_run_writes_schema_1_2_without_secret 断言 secret 不在 request.url；git ls-files 无 *.cookie/auth.json 类文件。 |
| A33 | passed | specs/local-auth-viewer-identity/spec.md | 错误处理只输出固定的脱敏身份/鉴权说明，不回显请求 headers、Cookie、认证文件内容或服务端可能包含敏感请求材料的 payload。 | _request_api 已移除服务端 message/msg 回显，_http_get 与 _resolve_viewer 仅输出固定错误文本，details 只含 scope/状态码等非敏感字段；测试断言异常 str/repr/details 不含 secret。 |
| A34 | passed | specs/local-auth-viewer-identity/spec.md | 自动测试必须使用明显唯一但完全虚假的 secret，并递归断言所有可观察输出都不含该 secret。 | UNIQUE_SECRET 为明显唯一且完全虚假的标记（opdeepseekflash_m2_fake_secret_7f3a9c21），测试对 JSON 输出、stdout/stderr、repr、异常文本与请求 URL 递归断言不含该 secret（test_viewer/test_output/test_cli 多处）。 |
| A35 | passed | specs/local-auth-viewer-identity/spec.md | 本能力不读取通知、不创建 SQLite/ledger、不计算跨运行可见性 diff、不实现写接口、登录 UI、扫码登录、浏览器自动化、自动刷新凭证、系统 keyring 或 LLM/MCP 上下文。 | 仓库仅新增 viewer/session/output 相关代码；grep 与文件清单确认无通知读取、SQLite/ledger、跨运行 diff、写接口、登录 UI/扫码、浏览器自动化、自动刷新凭证、keyring 或 LLM/MCP 上下文实现，roadmap 将 M3+ 标记为 planned。 |

## Checks

| Check | Command | Working directory | Status | Exit | Duration |
| --- | --- | --- | --- | ---: | ---: |
| uv run ruff format --check . | run ruff format --check . | . | passed | 0 | 107 ms |
| uv run ruff check . | run ruff check . | . | passed | 0 | 104 ms |
| uv run pytest -q --cov=auto_comment_reply --cov-report=term-missing | run pytest -q --cov=auto_comment_reply --cov-report=term-missing | . | passed | 0 | 1609 ms |

## Blockers

_None._

## Risks and skipped work

- 未做真实私人登录 nav smoke：登录响应字段契约仅由脱敏离线 fixture 覆盖，2026-08-16 只对匿名 nav 做过真实只读核验；若 B 站登录响应实际结构漂移，会沿 fail-closed 方向报错（exit 1）而不会误判身份，但登录字段契约仍需后续最小授权实测确认。
- 认证 legacy 整视频路径的可观察行为变化：nav 身份确认失败（含首请求网络故障）现在会在 legacy 诊断流程开始前 exit 1 且无 JSON，而匿名 legacy 仍保持原有 partial-result/exit 2 语义；这是 spec 对'提供凭证即要求登录视角'的 fail-closed 设计，但属于认证 legacy 的新增可观察行为。
- WBI force_refresh 边界：legacy 主评论遇 -352/-403 触发密钥轮换时 _get_mixin_key(force_refresh=True) 会再次请求 nav 并覆盖 _nav_payload；这是 M1 既有的密钥轮换行为而非身份请求，已缓存的 _viewer 不受影响，A2/A28 的'一次身份请求'保证仍成立，但该边缘路径会出现第二次 nav HTTP 请求。
- A6 字面边界：唯一标记假凭证常量存在于 tests/_helpers.py（归档后为已跟踪文件）并被测试导入；它是 A34 强制要求的脱敏 fixture，且完全虚假、带唯一标记，未泄漏到任何输出，但若对 A6 的'已跟踪文件'做最严格字面解读，这是唯一紧张点。
- docs/comet/changes/local-auth-viewer-identity/verification.md 不存在：第一轮失败报告实际保存在 comet-state.yaml 的 history 中，本次以 state 内记录为据；不影响实现验收，但文档路径与预期不一致。

## Previous iterations

| Goal cycle | Iteration | Attempt | Outcome | Unresolved | Summary | Completed |
| ---: | ---: | ---: | --- | --- | --- | --- |
| 1 | 1 | 1 | fail | A18 | 除 A18 外，A1-A17、A19-A35 全部通过，Runtime 三项检查（format/lint/pytest+coverage）均记录 exit 0。唯一阻塞项是 A18：Viewer 的 frozen dataclass 默认 __eq__/__hash__ 把 username 纳入相等性与哈希，直接违反 spec 明确要求 username 不参与相等性；虽然当前没有调用路径，验收按完整规格判定为失败。修复极小且明确：eq=False 并定义仅基于 (platform, authenticated, platform_user_id) 的 __eq__/__hash__（或等价 identity equality 语义），并补充相同身份不同 username 相等/哈希相同的回归测试。下一轮 Build 修复该模型契约并复跑三项 Runtime 检查即可通过。总 verdict=fail。 | 2026-08-16T08:59:33.064Z |
| 1 | 2 | 1 | pass | — | A18 已真正修复：Viewer 改为 eq=False 并自定义基于 (platform, authenticated, platform_user_id) 的 __eq__/__hash__，username 仅保留在 to_dict 展示，不参与相等性、哈希、discussion identity、同步范围、is_self 派生或任何授权判断，并新增三组回归测试。静态语义审阅复核 A1-A35：认证 fail-closed、nav 缓存与 legacy WBI 共享一次响应、schema 1.2 的 viewer/author_id/三态 is_self、legacy 1.0 不回归、秘密不泄漏、无越界功能均成立。Runtime 三项检查均 passed/exit 0（ruff format --check、ruff check、pytest -q --cov=auto_comment_reply --cov-report=term-missing；主流程观测 168 passed、总覆盖率 90%）。全部 35 项验收 passed，verdict=pass。 | 2026-08-16T09:21:27.297Z |

## Conclusion

A18 已真正修复：Viewer 改为 eq=False 并自定义基于 (platform, authenticated, platform_user_id) 的 __eq__/__hash__，username 仅保留在 to_dict 展示，不参与相等性、哈希、discussion identity、同步范围、is_self 派生或任何授权判断，并新增三组回归测试。静态语义审阅复核 A1-A35：认证 fail-closed、nav 缓存与 legacy WBI 共享一次响应、schema 1.2 的 viewer/author_id/三态 is_self、legacy 1.0 不回归、秘密不泄漏、无越界功能均成立。Runtime 三项检查均 passed/exit 0（ruff format --check、ruff check、pytest -q --cov=auto_comment_reply --cov-report=term-missing；主流程观测 168 passed、总覆盖率 90%）。全部 35 项验收 passed，verdict=pass。
