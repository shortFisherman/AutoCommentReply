# 架构：当前实现画像

> 本文档只描述项目**现在**的代码事实：当前运行形态、模块边界、数据流、领域模型、算法、端点、错误语义、输出格式与测试证据。全文都是当前态，没有“目标/当前基线”双层结构，也不重复标注【当前】。
>
> 项目为什么存在、用户旅程与常青原则见 [project.markdown](project.markdown)；未来方向、里程碑与依赖顺序见 [roadmap.markdown](roadmap.markdown)；参考项目调研见 [REFERENCE_RESEARCH.md](REFERENCE_RESEARCH.md)。

## 1. 文档职责与当前范围

- 本文档是“现在这个项目的画像”，只写当前已实现的代码事实；不承担项目意图/用户旅程（project 职责），也不承担未来规划/里程碑/目标架构（roadmap 职责）。
- 当前范围：只读读取 Bilibili 评论区。同一个 CLI 入口 `auto-comment-reply` 按输入自动分流为两条读路径——评论分享链接的**定向讨论同步**（M1，输出 schema 1.1）与**legacy 整视频读取**（输出 schema 1.0）。
- 运行环境：Python 3.11+；运行时第三方依赖仅 `httpx`（`httpx>=0.27,<1`）；CLI 用 `argparse`，模型用 `dataclasses`。平台仅 Bilibili，行为只读，输出 JSON（stdout 或文件）。

## 2. 当前运行形态

**定向讨论读取（M1，当前 MVP，schema 1.1）**

- 输入：评论分享链接（b23.tv 短链或展开后的 bilibili.com URL，带 `comment_root_id` / `comment_secondary_id` / `#reply` 标记之一）。
- 行为：把入口评论（可以是根评论或楼中楼）归约到它所属的根楼层，只读取该根评论与楼层内当前可见回复；不翻整段视频的根评论列表。入口焦点只记录，不改变同步范围，也不当作 parent。
- 输出：schema 1.1，额外含 `discussion`（规范化讨论身份与焦点）。

**legacy 整视频只读（旧 MVP 保留，仅诊断兼容，schema 1.0）**

- 输入：视频引用（BV 号、av/AV 号、纯数字 aid、视频 URL、b23.tv 视频短链）。
- 行为：翻取该视频当前可见的全部根评论，并为每个根评论读取其楼中楼，构建评论树与根到叶对话链。
- 输出：schema 1.0。

两条路径共用 `BilibiliAdapter.fetch_reference` 单一入口分流，均只读、均只输出 JSON，无任何写操作。

## 3. 模块/文件边界与依赖方向

| 文件 | 职责 | 依赖（本项目） |
| --- | --- | --- |
| `__main__.py` | 调用 `cli.main()` | cli |
| `__init__.py` | 包公开 API 导出，`__version__ = "0.1.0"` | 各子模块 |
| `cli.py` | argparse 参数、Cookie 读取（文件/BILIBILI_COOKIE）、构造 Adapter、生成并写输出、退出码 | adapter, errors, output |
| `adapter.py` | `BilibiliAdapter`：唯一允许知道 B 站私有网页 API 细节的模块；输入分流、b23 安全展开、视频归一化、分页、WBI 签名、评论规范化、诊断 | models, reference, wbi, errors, httpx |
| `reference.py` | 展开后评论分享链接的纯解析：`CommentReference`、`DiscussionReference`、`parse_comment_reference`、`build_discussion_reference`、`validate_url_authority`；无网络 | models, errors |
| `wbi.py` | `derive_mixin_key`、`sign_wbi_params`；纯签名计算 | 仅标准库 |
| `models.py` | 平台中立的数据类：`Comment`、`VideoInfo`、`Diagnostic`、`FetchStats`、`FetchResult` | 无（`DiscussionReference` 仅 TYPE_CHECKING） |
| `tree.py` | 图校验与建树：`CommentGraphError`、`CommentNode`、`TreeBuildResult`、`build_comment_forest`、`trace_to_root`、对话链 | models |
| `output.py` | `SCHEMA_VERSION_LEGACY="1.0"` / `SCHEMA_VERSION_DISCUSSION="1.1"`、`build_output_document`、`render_json` | models, tree |
| `errors.py` | 异常分类体系 `BilibiliError` 及子类 | 无 |

依赖方向单向、无环：

```text
__main__ → cli → adapter → httpx（唯一网络出口）
              │        ├→ reference（纯解析）
              │        ├→ wbi（纯签名）
              │        └→ models / errors
              └→ output → tree → models
```

- 网络 I/O 只存在于 `adapter.py`；文件/控制台 I/O 只存在于 `cli.py`。
- B 站私有接口的路径、参数、字段与 WBI 细节只允许存在于 adapter/wbi 边界；`reference.py`、`tree.py`、`output.py`、`models.py` 对平台线协议无感知。

## 4. CLI 路由与两条端到端数据流

`fetch_reference(reference)` 的分流规则：

1. 输入去空白；为空则 `ParameterError`。
2. 无 scheme 的输入补 `https://` 前缀后取 hostname。
3. `b23.tv`：先安全展开短链；若**原始短链或展开后 URL** 任一含评论标记 → 定向模式（用展开后 URL）；都不含 → legacy 模式（用展开后 URL）。
4. 允许的 Bilibili 域名（`bilibili.com`/`www`/`m` 或 `*.bilibili.com`）：含评论标记 → 定向；否则 → legacy。
5. 其他输入（含裸 BV/av/aid 与非 Bilibili 地址）→ legacy `fetch`，由其视频标识解析拒绝非法引用。

评论标记 = query 中键名含 `comment_root_id` 或 `comment_secondary_id`（大小写不敏感），或非空 fragment 以 `reply` 开头（大小写不敏感）。路由绝不静默降级：只要含标记就进定向模式，解析失败就 fail closed（exit 1），不会回退到整视频全量。

**定向端到端数据流（M1）**

```text
评论分享链接（b23.tv 或展开 URL）
 → b23 短链安全展开（如为短链）
 → parse_comment_reference：comment_root_id（必需）、comment_secondary_id、#reply；
   追踪参数（share_tag/unique_k/vd_source 等）忽略；focus = comment_secondary_id 或 #reply（冲突 fail closed）
 → resolve_video：GET /x/web-interface/view（bvid 或 aid），并校验链接内视频标识与视频元数据一致
 → DiscussionReference(platform="bilibili", object_type="video", aid, bvid, root_comment_id, focus_comment_id)
 → GET /x/v2/reply/reply：oid=aid、type=1、root=root_comment_id、pn=1、ps=20
   一次取得 data.root 与第 1 页 replies
 → 根有效性检查；第 1 页外部根回复排除
 → 从 pn=2 续页，直到终止条件或安全上限
 → 规范化、按 comment_id 合并、建树、对话链
 → schema 1.1 JSON（含 discussion）
```

定向路径不调用 `nav`、不请求主评论 `main`、不做 WBI 签名，`stats.root_pages_fetched` 恒为 0。

**legacy 端到端数据流**

```text
视频引用（BV/av/aid/视频 URL/b23.tv 视频短链）
 → resolve_video：GET /x/web-interface/view
 → GET /x/web-interface/nav 取 wbi_img → mixin key（缓存 600 秒）
 → GET /x/v2/reply/wbi/main：mode=3 + pagination_str 游标 + WBI 签名（wts/w_rid）
   游标分页全部根评论；cursor.is_end 为终止信号；top_replies 与内嵌预览一并解析
 → 对每个 rcount>0 或带内嵌预览的根评论：GET /x/v2/reply/reply 从 pn=1 楼中楼分页
 → 分支失败隔离：单个楼中楼失败记诊断、complete=false，其余分支继续
 → 规范化、按 comment_id 合并、建树、对话链
 → schema 1.0 JSON
```

## 5. 引用解析与 b23 安全

`validate_url_authority`（引用解析与短链展开共用）：仅 http(s)；不得含 userinfo；端口仅允许 80/443/缺省；必须有有效 hostname。违例即 `ParameterError`（fail closed）。

`parse_comment_reference` 的解析契约：

- host 必须是 `bilibili.com` / `www.bilibili.com` / `m.bilibili.com` 或 `*.bilibili.com`；`b23.tv` 必须先展开才能解析。
- path 必须含 `/video/`；从中提取 BV（`BV[0-9A-Za-z]{10}`）或 av/数字 aid；两者都提不出即拒绝。
- `comment_root_id`：必需正整数、且只能出现一次；`comment_secondary_id`：可选正整数；`#reply`：必须整体匹配 `reply<正整数>`。
- `focus_comment_id = comment_secondary_id ?? #reply`；两者都存在但不相等 → `ParameterError`。
- 只读取上述参数；`share_tag / unique_k / vd_source` 等追踪参数不参与身份。
- 产出不可变 `CommentReference(bvid, aid, root_comment_id, secondary_comment_id, fragment_comment_id, focus_comment_id)`。

`build_discussion_reference(video, reference)`：链接中的 bvid/aid（如有）必须与 `resolve_video` 返回的元数据一致，否则拒绝；产出 `DiscussionReference`，其 `identity == (platform, object_type, aid, root_comment_id)`，与 `focus_comment_id` 无关。焦点只作为入口记录进入输出，不改变身份、同步范围、root/parent 或建树。

b23 安全展开（`_resolve_short_link`）：

- 最多 5 跳（`_MAX_SHORT_LINK_HOPS = 5`），只接受 301/302/303/307/308 的 `Location`。
- 每跳先校验：循环（seen 集合）拒绝、非 http(s)/userinfo/危险端口/缺主机名拒绝、缺 `Location` 或非跳转状态拒绝、协议畸形（`RemoteProtocolError`）拒绝。
- 链上第一个非 b23 地址必须是允许的 Bilibili 域名，外站立即拒绝——短链不能变成任意 URL/SSRF 入口。
- 拿到允许的终态 URL 后直接解析其 query/fragment 继续路由，**不请求终态 HTML 页面**。

## 6. 当前使用的 B 站端点与 HTTP/WBI 边界

| 用途 | 接口 | 使用的路径 |
| --- | --- | --- |
| 视频 BV/AV 归一化 | `GET https://api.bilibili.com/x/web-interface/view` | 定向 + legacy |
| WBI 密钥 | `GET https://api.bilibili.com/x/web-interface/nav` | 仅 legacy |
| 主评论游标分页 | `GET https://api.bilibili.com/x/v2/reply/wbi/main` | 仅 legacy |
| 根评论元数据 + 楼中楼分页 | `GET https://api.bilibili.com/x/v2/reply/reply` | 定向 + legacy |

参数事实：

- `view`：传 `bvid` 或 `aid`。
- 定向 `reply/reply`：`oid=aid, type=1, root=root_comment_id, pn=1, ps=20`，一次同时返回 `data.root` 与第 1 页 `replies`；之后 `pn=2,3,…`。
- legacy `main`：`oid=aid, type=1, mode=3, pagination_str={"offset":…}, plat=1, seek_rpid="", web_location=1315875`，参数排序后追加 `wts` 与 `w_rid`。
- legacy 楼中楼 `reply/reply`：同样 `oid/type=1/root/pn/ps=20`，从 `pn=1` 开始。

HTTP 边界：

- 全部为 GET，httpx `follow_redirects=False`——重定向只在 b23 展开里被显式、逐跳处理。
- 请求头含 Accept / Referer（`https://www.bilibili.com/`）/ User-Agent；可选 `Cookie` 文本头。无登录 session，只有 Cookie 文本。
- 响应必须是 JSON 对象，`code == 0` 才取 `data`；其余按第 10 节分类。

WBI 边界（仅 legacy 使用）：

- `nav` 的 `wbi_img.img_url/sub_url` 文件名 stem 派生 32 字符 mixin key，缓存 600 秒；匿名 `nav` 返回 `-101` 但带可用 `wbi_img` 是被允许的特例。
- 签名：过滤 `!'()*` 字符 → 按 key 排序 → urlencode → 追加 `wts` → `w_rid = md5(query + mixin_key)`。
- 主评论接口返回 `-403/-352` 时强制刷新一次 mixin key 后重试。
- 定向路径不调用 `nav`/`main`、不做 WBI 签名。

这些是 B 站网页端非公开接口，路径、参数、字段随时可能变化；细节只在 adapter/wbi 边界内替换，源码是唯一权威实现。

## 7. 当前领域/传输模型

全部为 `dataclasses`（注明者 `frozen=True`）：

| 模型 | 字段 | 说明 |
| --- | --- | --- |
| `Comment` | `comment_id, user_id, username, content, root_id, parent_id, created_at, video_id, reply_count=0` | 平台中立评论；`is_root` = `root_id == 0 and parent_id == 0` |
| `VideoInfo` | `aid, bvid, title, owner_id, owner_name, visible_comment_count_hint=None` | 视频元数据 |
| `Diagnostic` | `severity(info|warning|error), category, scope, message, details` | 诊断条目 |
| `FetchStats` | `root_pages_fetched, reply_pages_fetched, expected_total_comments, root_comments_fetched, reply_comments_fetched, total_comments_fetched, duplicate_comments_seen, orphan_comments, conversation_chains` | 运行计数；后两项在输出阶段由建树结果覆盖 |
| `FetchResult` | `video, comments, complete, diagnostics, stats, discussion=None` | 一次读取结果；`discussion` 非空即定向模式 |
| `CommentReference` | `bvid, aid, root_comment_id, secondary_comment_id, fragment_comment_id, focus_comment_id` | 展开链接解析结果（不可变） |
| `DiscussionReference` | `platform, object_type, aid, bvid, root_comment_id, focus_comment_id=None` | 规范讨论身份；`identity` 属性；`to_dict` 含 `platform/object_type/oid/aid/bvid/root_comment_id/focus_comment_id/identity` |

评论字段映射（Adapter 边界内完成）：

| 模型字段 | B 站字段 | 语义 |
| --- | --- | --- |
| `comment_id` | `rpid` | 评论唯一 ID，也是合并/去重键 |
| `user_id` | `member.mid` | 作者身份 |
| `username` | `member.uname` | 仅展示 |
| `content` | `content.message` | 正文 |
| `root_id` | `root` | 所属根评论 ID |
| `parent_id` | `parent` | 直接父评论 ID |
| `created_at` | `ctime` | Unix 秒时间戳 |
| `reply_count` | `rcount` | 接口提示的回复数 |
| `video_id` | 本次解析出的 `bvid` | 附到每条评论 |

根评论约定 `root_id == 0` 且 `parent_id == 0`。`rpid/root/parent` 缺失或非法时该评论被跳过并记 error；展示/身份字段缺失时使用安全占位（`user_id=0`、`username=""`、`content=""`、`created_at=0`）并记 error，结果不完整。

`VideoInfo` 映射：`aid←aid`、`bvid←bvid`、`title←title`、`owner_id←owner.mid`、`owner_name←owner.name`、`visible_comment_count_hint←stat.reply`。

## 8. 评论解析、去重、楼层分页、树/孤儿/对话链

解析与合并：

- Adapter 以 `comment_id` 为键 `_upsert_comment`：首次出现保留；重复出现计数 `duplicate_comments_seen`，非空字段覆盖占位值（`reply_count` 取大）；`root/parent` 冲突记 `relationship_conflict` error。
- legacy 的置顶（`top_replies`）与普通列表、根评论内嵌预览（`replies`）都走同一解析+合并路径，不产生重复节点。
- 建树层再以 `comment_id` 索引，保留首次出现记录，重复记 warning（`duplicate_comment`）。

楼层分页：

- 定向：`pn=1` 一次取得 `data.root` 与第 1 页；第 1 页中 `root_id` 不等于请求根的回复记 warning（`foreign_root_reply_excluded`）并排除，属于请求根的回复保留（根无效时仍保留，随后建树时成为孤儿，不丢数据）；从 `pn=2` 续页。终止条件：唯一回复数达到 `page.count`，或接口合法空页；提前空页 → `pagination_incomplete` error；重复页指纹 → `pagination_loop` error；达到 `--max-reply-pages` → `pagination_limit` error。根有效且计数可得时 `expected_total_comments = 1 + page.count`。
- legacy 主评论：`cursor.is_end`（true/1/"true"）为终止信号；`pagination_reply.next_offset` 续页；空页且未显式未结束视为终止；未结束却无 `next_offset` → `PaginationError`；offset 循环 → `PaginationError`；`cursor.all_count` 写入 `expected_total_comments`；达到 `--max-root-pages` → error。
- legacy 楼中楼：复用同一 `_fetch_reply_pages`（从 `pn=1` 起）；只请求 `rcount > 0` 或带内嵌预览的根评论；单分支失败隔离（该分支记 error、`complete=false`），其余分支继续。
- 计数漂移（唯一评论数与接口计数不一致）只记 warning（`count_changed`），不推翻已完成的分页。

树、孤儿与对话链（`tree.py`）：

- `build_comment_forest` 按 `parent_id` 建树；每个节点沿父链解析归属根，带循环检测。错根（`root_mismatch`）、缺父（`missing_parent`）、循环（`comment_cycle`）、非根节点 `root/parent == 0`（`invalid_relationship`）都记为 error，相关节点进入 `orphans`，不伪造边。
- 树与兄弟节点都按 `(created_at, comment_id)` 排序，重复运行结构稳定。
- `conversation_chains`：迭代 DFS 导出所有根到叶分支（`comment_id` 列表，根在前）。
- `trace_to_root`：沿直接父链追溯（根在前）；目标不存在、缺节点、循环、非根 `parent_id==0`、路径 `root_id` 与到达根不一致时抛 `CommentGraphError`，不静默截断。

## 9. 输出 schema：1.1 定向与 1.0 legacy

`build_output_document` 的 schema 版本规则：`FetchResult.discussion` 非空 → `"1.1"`，否则 → `"1.0"`。

公共顶层：

```text
schema_version           1.1=讨论定向；1.0=legacy 整视频
generated_at             生成时间，UTC，ISO8601 以 Z 结尾
complete                 见第 10 节
video                    aid/bvid/title/owner_id/owner_name/visible_comment_count_hint/url
stats                    FetchStats 全字段；orphan_comments 与 conversation_chains 由建树结果覆盖
comments                 规范化评论平面列表，按 (created_at, comment_id) 排序
trees                    [{"comment": {...}, "children": [...]}] 嵌套树
conversation_chains      每条根到叶分支的 comment_id 列表
orphan_comment_ids       缺父/错根/循环等无法可靠挂树的节点 ID
duplicate_comment_ids    建树层检测到的重复 comment_id
diagnostics              [{severity, category, scope, message, details}]
```

1.1 额外含 `discussion`：`platform / object_type / oid / aid / bvid / root_comment_id / focus_comment_id / identity`；`identity = {platform, object_type, oid, root_comment_id}`，与入口焦点无关。`focus_comment_id` 只记录入口焦点，不参与建树、不当 parent。

输出写盘用临时文件 + `os.replace` 原子替换；目标文件已存在且未 `--force` 时在读取前即退出。

## 10. complete / diagnostics / 退出码 / 重试与节流 / 安全上限

`complete`：

- Adapter：任一 error 级诊断即 `complete=false`；warning 不推翻。
- 输出阶段再校验一次：`complete = result.complete and 无 error 诊断`（把建树错误也纳入）。
- 定向根无效（`data.root` 缺失、`invisible=true`、rpid 与请求 root 不一致、root/parent 关系非根）都产生 error → `complete=false`，且不声称永久删除。
- `complete=true` 只表示：按接口明确终止信息读完了本次运行当前可见数据，并通过父链完整性校验；不承诺补全已删除/屏蔽/无权限内容。

`diagnostics`：每条含 `severity`（info/warning/error）、`category`、`scope`、`message`、`details`；error 使结果不完整，warning（计数漂移、外部根排除、重复 ID 等）不改变 complete。

退出码（`cli.py`）：

| 码 | 含义 |
| --- | --- |
| `0` | 已输出 JSON，且 `complete=true` |
| `2` | 已输出 JSON，但 `complete=false` |
| `1` | 读取前致命错误：输入/引用解析、focus 冲突、b23 安全拒绝、视频解析、Cookie 文件、输出文件等（无 JSON 输出） |

重试与节流：

- 网络/临时服务错误最多 `retries` 次重试（默认 2，即最多 3 次尝试），指数退避 `0.5 * 2^attempt` 秒；可重试的是连接错误、HTTP 429、HTTP ≥500。HTTP 403/412 立即转类型化错误，不重试。
- 相邻请求最小间隔 `request_delay`（默认 0.25 秒）由 `_pace_request` 保证。
- WBI：主评论 `-403/-352` 强制刷新一次密钥后重试。

安全上限：

- b23 最多 5 跳。
- `--max-root-pages`（默认 10000，仅 legacy 主评论生效；触发 → error、`complete=false`）。
- `--max-reply-pages`（默认 10000，两路径的楼中楼都生效；触发 → error、`complete=false`）。

错误分类（`errors.py`，经 `_request_api` 映射）：

| 异常 | 触发 |
| --- | --- |
| `NetworkError` | 连接/超时，重试耗尽后 |
| `AccessDeniedError` | HTTP 403，或 API `-352/-403/-412` |
| `RateLimitError` | HTTP 412/429，或 API `-799/-509`（`retryable=true`） |
| `HttpError` | 其余非成功 HTTP（5xx 重试耗尽后） |
| `AuthenticationError` | API `-101`（匿名 `nav` 取 WBI 密钥的 `-101` 特例除外） |
| `ParameterError` | API `-400`，以及引用解析/短链安全的输入拒绝 |
| `BusinessError` | `-404/100100404`、`12002`（评论区关闭）等业务码 |
| `ResponseParseError` | JSON/结构不符合预期 |
| `PaginationError` | 分页契约被破坏（游标循环、无 next_offset 等） |

## 11. 当前测试与真实 smoke 证据

- 测试文件：`tests/test_adapter.py`、`test_cli.py`、`test_reference.py`、`test_tree.py`、`test_wbi.py`；全部离线，用 `httpx.MockTransport`，自动测试不依赖真实网络。
- 覆盖：WBI 固定向量与签名、视频标识解析、主评论/楼中楼分页、置顶与内嵌预览去重、分支失败隔离、孤儿/错根/循环/重复 ID、网络重试、b23 安全跳转，以及定向模式的引用解析、讨论身份、focus 语义、定向分页、外部根排除、根无效语义、fail-closed 路由、CLI 退出码与 Cookie 处理、输出 schema。
- 本次运行实测：`129 passed`，总覆盖率 89%（adapter 83%、cli 90%、reference 99%、tree 94%、output 93%、wbi 92%、models/errors 100%）。
- 真实 smoke（记录于文档，非自动测试）：匿名定向 CLI smoke 一次——1 根评论 + 1 回复、`stats.root_pages_fetched=0`、`complete=true`；legacy 全量模式于 2026-08-14 做过真实只读验证。真实 B 站运行由项目所有者按需进行，不进入自动测试。

## 12. 当前已知限制

当前没有数据库、跨运行状态库或持续同步 ledger，也没有登录 session 或 viewer 身份（只有可选的 Cookie 文本头）、通知读取、LLM/MCP 上下文或评论写接口（含确认式/幂等发送）；每次运行都是进程内一次性只读，唯一可落盘的 JSON 只是导出产物，不作为跨运行状态存储。无法恢复从未观察到且已删除或屏蔽的评论。这些只是“当前不存在”的现状描述；未来方向与实施顺序由 [roadmap.markdown](roadmap.markdown) 承担。
