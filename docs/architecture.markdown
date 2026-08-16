# 架构：当前实现画像

> 本文档只描述项目**现在**的代码事实：当前运行形态、模块边界、数据流、领域模型、算法、端点、错误语义、输出格式与测试证据。全文都是当前态，没有“目标/当前基线”双层结构，也不重复标注【当前】。
>
> 项目为什么存在、用户旅程与常青原则见 [project.markdown](project.markdown)；未来方向、里程碑与依赖顺序见 [roadmap.markdown](roadmap.markdown)；参考项目调研见 [REFERENCE_RESEARCH.md](REFERENCE_RESEARCH.md)。

## 1. 文档职责与当前范围

- 本文档是“现在这个项目的画像”，只写当前已实现的代码事实；不承担项目意图/用户旅程（project 职责），也不承担未来规划/里程碑/目标架构（roadmap 职责）。
- 当前范围：只读读取 Bilibili 评论区。同一个 CLI 入口 `auto-comment-reply` 按输入自动分流为两条读路径——评论分享链接的**定向讨论同步**（M1）与**legacy 整视频读取**（旧 MVP，仅诊断兼容）。
- M2 已在定向链路上加入**本地进程内认证 session 与 viewer 身份**：定向输出升级为 schema 1.2（顶层 `viewer`、`author_id` + 三态 `is_self`）；legacy 输出保持 schema 1.0 不变。
- 运行环境：Python 3.11+；运行时第三方依赖仅 `httpx`（`httpx>=0.27,<1`）；CLI 用 `argparse`，模型用 `dataclasses`。平台仅 Bilibili，行为只读，输出 JSON（stdout 或文件）。

## 2. 当前运行形态

**定向讨论读取（M1 + M2，当前 MVP，schema 1.2）**

- 输入：评论分享链接（b23.tv 短链或展开后的 bilibili.com URL，带 `comment_root_id` / `comment_secondary_id` / `#reply` 标记之一）；可选认证输入 `--cookie-file` 或 `BILIBILI_COOKIE`（前者优先）。
- 行为：把入口评论（可以是根评论或楼中楼）归约到它所属的根楼层，只读取该根评论与楼层内当前可见回复；不翻整段视频的根评论列表。入口焦点只记录，不改变同步范围，也不当作 parent。
- viewer 视角：无凭证时创建显式 anonymous viewer（`authenticated=false`、`platform_user_id=null`、`username=null`），**不因身份识别请求 nav**；有凭证时在评论读取前用一次 `GET /x/web-interface/nav` 确认登录 viewer，解析结果在 Adapter 生命周期内缓存并与 legacy WBI 取 key 共用。身份无法确认时 fail closed（exit 1，无 JSON），不静默降级为匿名。
- 输出：schema 1.2，额外含 `discussion` 与顶层 `viewer`；定向 `comments`/`trees` 的作者字段为 `author_id`，并带输出期派生的三态 `is_self`。

**legacy 整视频只读（旧 MVP 保留，仅诊断兼容，schema 1.0）**

- 输入：视频引用（BV 号、av/AV 号、纯数字 aid、视频 URL、b23.tv 视频短链）。
- 行为：翻取该视频当前可见的全部根评论，并为每个根评论读取其楼中楼，构建评论树与根到叶对话链。
- 输出：schema 1.0，字段与行为不变（`user_id` 保留，不含 `viewer`/`author_id`/`is_self`）。提供凭证时同样先确认 viewer（与 WBI 取 key 共用一次 nav）；匿名时仍只为 WBI 取 key 请求 nav（`code=-101` 特例）。

两条路径共用 `BilibiliAdapter.fetch_reference` 单一入口分流，均只读、均只输出 JSON，无任何写操作。

## 3. 模块/文件边界与依赖方向

| 文件 | 职责 | 依赖（本项目） |
| --- | --- | --- |
| `__main__.py` | 调用 `cli.main()` | cli |
| `__init__.py` | 包公开 API 导出，`__version__ = "0.1.0"` | 各子模块 |
| `cli.py` | argparse 参数、Cookie 读取（`--cookie-file` 优先，否则 `BILIBILI_COOKIE`）、构造 Adapter、生成并写输出、退出码 | adapter, errors, output |
| `adapter.py` | `BilibiliAdapter`：唯一允许知道 B 站私有网页 API 细节的模块；输入分流、b23 安全展开、视频归一化、分页、WBI 签名、本地认证 session、viewer 解析与缓存、评论规范化、诊断 | models, reference, wbi, errors, httpx |
| `reference.py` | 展开后评论分享链接的纯解析：`CommentReference`、`DiscussionReference`、`parse_comment_reference`、`build_discussion_reference`、`validate_url_authority`；无网络 | models, errors |
| `wbi.py` | `derive_mixin_key`、`sign_wbi_params`；纯签名计算 | 仅标准库 |
| `models.py` | 平台中立的数据类：`Comment`、`VideoInfo`、`Viewer`、`ANONYMOUS_VIEWER`、`Diagnostic`、`FetchStats`、`FetchResult` | 无（`DiscussionReference` 仅 TYPE_CHECKING） |
| `tree.py` | 图校验与建树：`CommentGraphError`、`CommentNode`、`TreeBuildResult`、`build_comment_forest`、`trace_to_root`、对话链 | models |
| `output.py` | `SCHEMA_VERSION_LEGACY="1.0"` / `SCHEMA_VERSION_DISCUSSION="1.2"`、`derive_is_self`、`build_output_document`、`render_json` | models, tree |
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
- 凭证只在 CLI 输入边界进入进程，由 Adapter 以 `Cookie` header 注入允许的 Bilibili 主机；不进入 URL、argv、模型或输出。
- B 站私有接口的路径、参数、字段与 WBI 细节只允许存在于 adapter/wbi 边界；`reference.py`、`tree.py`、`output.py`、`models.py` 对平台线协议无感知。

## 4. CLI 路由与两条端到端数据流

`fetch_reference(reference)` 的分流规则：

1. 输入去空白；为空则 `ParameterError`。
2. 无 scheme 的输入补 `https://` 前缀后取 hostname。
3. `b23.tv`：先安全展开短链；若**原始短链或展开后 URL** 任一含评论标记 → 定向模式（用展开后 URL）；都不含 → legacy 模式（用展开后 URL）。
4. 允许的 Bilibili 域名（`bilibili.com`/`www`/`m` 或 `*.bilibili.com`）：含评论标记 → 定向；否则 → legacy。
5. 其他输入（含裸 BV/av/aid 与非 Bilibili 地址）→ legacy `fetch`，由其视频标识解析拒绝非法引用。

评论标记 = query 中键名含 `comment_root_id` 或 `comment_secondary_id`（大小写不敏感），或非空 fragment 以 `reply` 开头（大小写不敏感）。路由绝不静默降级：只要含标记就进定向模式，解析失败就 fail closed（exit 1），不会回退到整视频全量。

**定向端到端数据流（M1 + M2）**

```text
评论分享链接（b23.tv 或展开 URL）
 → b23 短链安全展开（如为短链）
 → parse_comment_reference：comment_root_id（必需）、comment_secondary_id、#reply；
   追踪参数（share_tag/unique_k/vd_source 等）忽略；focus = comment_secondary_id 或 #reply（冲突 fail closed）
 → viewer 解析（评论读取前）：
    认证 session → GET /x/web-interface/nav（scope=viewer_identity）一次，
      解析 isLogin=true + 正整数 mid（可空 string 型 uname 仅展示）；失败 fail closed，exit 1 无 JSON
    匿名 session → ANONYMOUS_VIEWER，不请求 nav
 → resolve_video：GET /x/web-interface/view（bvid 或 aid），并校验链接内视频标识与视频元数据一致
 → DiscussionReference(platform="bilibili", object_type="video", aid, bvid, root_comment_id, focus_comment_id)
 → GET /x/v2/reply/reply：oid=aid、type=1、root=root_comment_id、pn=1、ps=20
   一次取得 data.root 与第 1 页 replies
 → 根有效性检查；第 1 页外部根回复排除
 → 从 pn=2 续页，直到终止条件或安全上限
 → 规范化、按 comment_id 合并、建树、对话链
 → schema 1.2 JSON（discussion + viewer；comments/trees 为 author_id + is_self）
```

定向路径不调用主评论 `main`、不做 WBI 签名，`stats.root_pages_fetched` 恒为 0；认证 session 只新增每个 Adapter 生命周期至多一次、可与 legacy WBI 共用的 nav 身份请求。

**legacy 端到端数据流**

```text
视频引用（BV/av/aid/视频 URL/b23.tv 视频短链）
 → viewer 解析：认证 session 先 GET nav 确认 viewer（复用同一 payload 供 WBI 取 key）；
   匿名 session → ANONYMOUS_VIEWER（不因身份请求 nav）
 → resolve_video：GET /x/web-interface/view
 → GET /x/web-interface/nav 取 wbi_img → mixin key（认证时复用身份请求已缓存的 payload；匿名时允许 code=-101；缓存 600 秒）
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

## 6. 本地认证 session 与 viewer 身份（M2）

**认证输入与 session 边界**

- 认证输入只沿用 `--cookie-file`（本机私有文件，优先）与 `BILIBILI_COOKIE` 环境变量；不新增 `auth.json`、`--auth-file`、默认凭证路径、argv 明文 Cookie 或跨运行认证状态。
- Cookie 文件去除首尾空白后为空、仍含内部换行或不可读 → `ValueError`，exit 1，发生在任何网络读取之前。
- `--cookie-file` 读取约定：文件按 UTF-8（含 UTF-8 BOM）读取，内容为一条逻辑行，首尾空白被 strip；文件优先于 `BILIBILI_COOKIE`，推荐放在工作区之外（如 `$env:LOCALAPPDATA\AutoCommentReply`）。凭证文件的创建与保管属于本机操作与安全边界：Agent/模型只应传递文件路径与任务输入，不读取或回显凭证内容（实现本身无法强制阻止进程外 Agent 读取本机文件）。
- 凭证只存在于：本机认证输入、Adapter 生命周期内的进程内字段、发往允许 Bilibili 主机的 HTTP `Cookie` header。`_authenticated_session` 只记录“是否提供了凭证”；关闭 Adapter（`close`/`__exit__`）即结束 session。
- 无凭证时 `_authenticated_session=false`，viewer 固定为模块级 `ANONYMOUS_VIEWER`，不请求 nav。

**viewer 解析与缓存**

- `_resolve_viewer()`：viewer 已解析则直接复用；匿名则返回 `ANONYMOUS_VIEWER`；认证则读取或发起一次 `GET /x/web-interface/nav`（scope=`viewer_identity`，默认只接受 `code=0`），把 `data` 存入 `_nav_payload` 后由 `_parse_viewer` 解析。
- `_parse_viewer` 严格契约：`isLogin is True`（JSON boolean true）；`mid` 必须是正整数（int 或纯十进制数字字符串；bool/float/负/零/小数/字母均拒绝）；`uname` 必须是 `null` 或 string。解析产物 `Viewer(platform="bilibili", authenticated=true, platform_user_id=mid, username=uname)` 不含任何凭证。
- 同一 Adapter 生命周期内 `_viewer` 与 `_nav_payload` 均缓存；legacy WBI 取 key 时若 `_nav_payload` 已存在则直接复用，不再发起第二次 nav（只有 WBI 密钥强制刷新路径例外）。
- 认证失败语义：nav 返回 `-101`、`isLogin != true`、mid 缺失/非法、响应结构无效或网络/服务错误 → 类型化 `AuthenticationError`/`ResponseParseError`，发生在评论读取前；CLI exit 1，stdout 与磁盘都不产生 JSON，评论读取不会开始。绝不静默降级为匿名。

**secret 边界**

- 凭证不得出现在 stdout、输出文件、stderr/verbose 日志、异常消息与 repr、diagnostics/details、文档或任何可提交产物；错误信息是固定的脱敏文本，不回显 Cookie、请求 headers 或服务端 payload。
- 自动测试使用明显唯一但完全虚假的 secret，并递归断言所有可观察输出（JSON、repr、日志、异常）都不含该 secret。

## 7. 当前使用的 B 站端点与 HTTP/WBI 边界

| 用途 | 接口 | 使用的路径 |
| --- | --- | --- |
| 视频 BV/AV 归一化 | `GET https://api.bilibili.com/x/web-interface/view` | 定向 + legacy |
| viewer 身份确认（M2，仅认证 session） | `GET https://api.bilibili.com/x/web-interface/nav` | 定向（认证）+ legacy（认证） |
| WBI 密钥 | `GET https://api.bilibili.com/x/web-interface/nav` | 仅 legacy（匿名 `code=-101` 特例；认证时与身份请求共用同一次响应） |
| 主评论游标分页 | `GET https://api.bilibili.com/x/v2/reply/wbi/main` | 仅 legacy |
| 根评论元数据 + 楼中楼分页 | `GET https://api.bilibili.com/x/v2/reply/reply` | 定向 + legacy |

参数事实：

- `view`：传 `bvid` 或 `aid`。
- 定向 `reply/reply`：`oid=aid, type=1, root=root_comment_id, pn=1, ps=20`，一次同时返回 `data.root` 与第 1 页 `replies`；之后 `pn=2,3,…`。
- legacy `main`：`oid=aid, type=1, mode=3, pagination_str={"offset":…}, plat=1, seek_rpid="", web_location=1315875`，参数排序后追加 `wts` 与 `w_rid`。
- legacy 楼中楼 `reply/reply`：同样 `oid/type=1/root/pn/ps=20`，从 `pn=1` 开始。

HTTP 边界：

- 全部为 GET，httpx `follow_redirects=False`——重定向只在 b23 展开里被显式、逐跳处理。
- 请求头含 Accept / Referer（`https://www.bilibili.com/`）/ User-Agent；可选 `Cookie` 头只来自 `--cookie-file`/`BILIBILI_COOKIE` 的凭证。没有其他认证状态、keyring、浏览器会话或持久化文件。
- 响应必须是 JSON 对象，`code == 0` 才取 `data`；其余按第 11 节分类。

WBI 边界（仅 legacy 使用）：

- `nav` 的 `wbi_img.img_url/sub_url` 文件名 stem 派生 32 字符 mixin key，缓存 600 秒；匿名 `nav` 返回 `-101` 但带可用 `wbi_img` 是被允许的特例。
- 签名：过滤 `!'()*` 字符 → 按 key 排序 → urlencode → 追加 `wts` → `w_rid = md5(query + mixin_key)`。
- 主评论接口返回 `-403/-352` 时强制刷新一次 mixin key 后重试。
- 定向路径不调用 `main`、不做 WBI 签名。

这些是 B 站网页端非公开接口，路径、参数、字段随时可能变化；细节只在 adapter/wbi 边界内替换，源码是唯一权威实现。

## 8. 当前领域/传输模型

全部为 `dataclasses`（注明者 `frozen=True`）：

| 模型 | 字段 | 说明 |
| --- | --- | --- |
| `Comment` | `comment_id, user_id, username, content, root_id, parent_id, created_at, video_id, reply_count=0` | 平台中立评论；`is_root` = `root_id == 0 and parent_id == 0`；**不含 `is_self`**（输出期派生） |
| `VideoInfo` | `aid, bvid, title, owner_id, owner_name, visible_comment_count_hint=None` | 视频元数据 |
| `Viewer`（frozen） | `platform, authenticated, platform_user_id, username` | 无凭证 viewer 快照；认证 viewer 必须有正整数 `platform_user_id`，username 可空仅展示；anonymous 不得携带身份字段 |
| `ANONYMOUS_VIEWER` | — | 模块级常量：`bilibili / false / null / null` |
| `Diagnostic` | `severity(info|warning|error), category, scope, message, details` | 诊断条目 |
| `FetchStats` | `root_pages_fetched, reply_pages_fetched, expected_total_comments, root_comments_fetched, reply_comments_fetched, total_comments_fetched, duplicate_comments_seen, orphan_comments, conversation_chains` | 运行计数；后两项在输出阶段由建树结果覆盖 |
| `FetchResult` | `video, comments, complete, diagnostics, stats, discussion=None, viewer=ANONYMOUS_VIEWER` | 一次读取结果；`discussion` 非空即定向模式 |
| `CommentReference`（frozen） | `bvid, aid, root_comment_id, secondary_comment_id, fragment_comment_id, focus_comment_id` | 展开链接解析结果 |
| `DiscussionReference`（frozen） | `platform, object_type, aid, bvid, root_comment_id, focus_comment_id=None` | 规范讨论身份；`identity` 属性；`to_dict` 含 `platform/object_type/oid/aid/bvid/root_comment_id/focus_comment_id/identity` |

评论字段映射（Adapter 边界内完成）：

| 模型字段 | B 站字段 | 语义 |
| --- | --- | --- |
| `comment_id` | `rpid` | 评论唯一 ID，也是合并/去重键 |
| `user_id` | `member.mid` | 作者稳定身份；定向 schema 1.2 输出为 `author_id`，legacy schema 1.0 输出为 `user_id` |
| `username` | `member.uname` | 仅展示，不参与任何身份比较 |
| `content` | `content.message` | 正文 |
| `root_id` | `root` | 所属根评论 ID |
| `parent_id` | `parent` | 直接父评论 ID |
| `created_at` | `ctime` | Unix 秒时间戳 |
| `reply_count` | `rcount` | 接口提示的回复数 |
| `video_id` | 本次解析出的 `bvid` | 附到每条评论 |

根评论约定 `root_id == 0` 且 `parent_id == 0`。`rpid/root/parent` 缺失或非法时该评论被跳过并记 error；展示/身份字段缺失时使用安全占位（`user_id=0`、`username=""`、`content=""`、`created_at=0`）并记 error，结果不完整。

`VideoInfo` 映射：`aid←aid`、`bvid←bvid`、`title←title`、`owner_id←owner.mid`、`owner_name←owner.name`、`visible_comment_count_hint←stat.reply`。

## 9. 评论解析、去重、楼层分页、树/孤儿/对话链

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

## 10. 输出 schema：1.2 定向与 1.0 legacy

`build_output_document` 的 schema 版本规则：`FetchResult.discussion` 非空 → `"1.2"`，否则 → `"1.0"`。

公共顶层：

```text
schema_version           1.2=讨论定向；1.0=legacy 整视频
generated_at             生成时间，UTC，ISO8601 以 Z 结尾
complete                 见第 11 节
video                    aid/bvid/title/owner_id/owner_name/visible_comment_count_hint/url
stats                    FetchStats 全字段；orphan_comments 与 conversation_chains 由建树结果覆盖
comments                 规范化评论平面列表，按 (created_at, comment_id) 排序
trees                    [{"comment": {...}, "children": [...]}] 嵌套树
conversation_chains      每条根到叶分支的 comment_id 列表
orphan_comment_ids       缺父/错根/循环等无法可靠挂树的节点 ID
duplicate_comment_ids    建树层检测到的重复 comment_id
diagnostics              [{severity, category, scope, message, details}]
```

定向 1.2 额外字段与差异：

- 顶层 `viewer`：`{platform, authenticated, platform_user_id, username}`；anonymous 时 `authenticated=false`、`platform_user_id=null`、`username=null`。
- 顶层 `discussion`：`platform / object_type / oid / aid / bvid / root_comment_id / focus_comment_id / identity`；`identity = {platform, object_type, oid, root_comment_id}`，与入口焦点和 viewer 均无关。`focus_comment_id` 只记录入口焦点，不参与建树、不当 parent。
- `comments` 与 `trees` 中的每条评论只输出 `author_id`（来自事实模型 `user_id`），**不再输出 `user_id` 兼容别名**；schema 1.1 消费者必须按版本显式迁移。
- 每条评论/树节点输出三态 `is_self`：viewer 已认证且作者身份已知时，`author_id == viewer.platform_user_id` → `true`，否则 `false`；viewer 匿名或作者身份未知（`user_id=0` 占位）→ `null`，不得用 `false` 伪装未知。`is_self` 只由 `derive_is_self` 在输出阶段派生，不存入 `Comment` 事实模型。
- 讨论身份、同步范围、`stats.root_pages_fetched == 0` 不随 viewer 变化。

legacy 1.0 保持原契约：无 `viewer`，评论/树节点使用 `user_id`，不含 `author_id`/`is_self`。

输出写盘用临时文件 + `os.replace` 原子替换；目标文件已存在且未 `--force` 时在读取前即退出。

## 11. complete / diagnostics / 退出码 / 重试与节流 / 安全上限

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
| `1` | 读取前致命错误：输入/引用解析、focus 冲突、b23 安全拒绝、视频解析、Cookie 文件、输出文件，以及**提供凭证但身份无法确认**（nav 未登录 / mid 非法 / 结构无效 / 身份请求失败）——均无 JSON 输出 |

重试与节流：

- 网络/临时服务错误最多 `retries` 次重试（默认 2，即最多 3 次尝试），指数退避 `0.5 * 2^attempt` 秒；可重试的是连接错误、HTTP 429、HTTP ≥500。HTTP 403/412 立即转类型化错误，不重试。
- 相邻请求最小间隔 `request_delay`（默认 0.25 秒）由 `_pace_request` 保证。
- WBI：主评论 `-403/-352` 强制刷新一次密钥后重试。

安全上限：

- b23 最多 5 跳。
- `--max-root-pages`（默认 10000，仅 legacy 主评论生效；触发 → error、`complete=false`）。
- `--max-reply-pages`（默认 10000，两路径的楼中楼都生效；触发 → error、`complete=false`）。

错误分类（`errors.py`，经 `_request_api` 与 viewer 解析映射）：

| 异常 | 触发 |
| --- | --- |
| `NetworkError` | 连接/超时，重试耗尽后 |
| `AccessDeniedError` | HTTP 403，或 API `-352/-403/-412` |
| `RateLimitError` | HTTP 412/429，或 API `-799/-509`（`retryable=true`） |
| `HttpError` | 其余非成功 HTTP（5xx 重试耗尽后） |
| `AuthenticationError` | API `-101`（匿名 `nav` 取 WBI 密钥的 `-101` 特例除外）；认证 session 的 nav 未登录、身份请求失败或服务错误 |
| `ParameterError` | API `-400`，以及引用解析/短链安全的输入拒绝 |
| `BusinessError` | `-404/100100404`、`12002`（评论区关闭）等业务码 |
| `ResponseParseError` | JSON/结构不符合预期，或认证 nav 缺少合法 `isLogin/mid/uname` 结构 |
| `PaginationError` | 分页契约被破坏（游标循环、无 next_offset 等） |

所有错误消息与 `details` 均为固定脱敏文本，不回显 Cookie、headers 或服务端敏感 payload。

## 12. 当前测试与真实验证证据

- 测试文件：`tests/test_adapter.py`、`test_cli.py`、`test_output.py`、`test_reference.py`、`test_tree.py`、`test_viewer.py`、`test_wbi.py`，以及 M2 共享脱敏 fixture `tests/_helpers.py`；全部离线，用 `httpx.MockTransport`，自动测试不依赖真实网络。
- 覆盖：WBI 固定向量与签名、视频标识解析、主评论/楼中楼分页、置顶与内嵌预览去重、分支失败隔离、孤儿/错根/循环/重复 ID、网络重试、b23 安全跳转，以及定向模式的引用解析、讨论身份、focus 语义、定向分页、外部根排除、根无效语义、fail-closed 路由、CLI 退出码与 Cookie 处理、输出 schema；M2 新增匿名/有效登录/失效登录、viewer 解析、`is_self` 三态、nav 请求预算（至多一次且与 legacy WBI 共用）与 secret 泄漏路径（stdout/文件/stderr/repr/异常/JSON）。
- 本次运行实测：`uv run pytest -q --cov=auto_comment_reply --cov-report=term-missing` 为 **168 passed**，总覆盖率 **90%**（adapter 85%、cli 91%、output 95%、models 97%、reference 99%、tree 94%、wbi 92%、errors 与 `__init__` 100%、`__main__` 0%）；`uv run ruff format --check .` 与 `uv run ruff check .` 均通过。
- 真实只读验证（记录于文档，非自动测试）：
  - 2026-08-16 匿名 nav 只读核验：返回 `code=-101`、`isLogin=false`、`mid=null`、`uname=null`，且仍含可用的 `wbi_img`（匿名取 WBI 密钥的合法形态）。
  - 登录态**只**由脱敏离线 fixture 验证（`tests/_helpers.py` / `test_viewer.py` / `test_output.py` / `test_cli.py`），未使用真实私人账号 smoke；不得伪称已做真实登录验证。
  - 早期匿名定向 CLI smoke 一次：1 根评论 + 1 回复、`stats.root_pages_fetched=0`、`complete=true`；legacy 全量模式于 2026-08-14 做过真实只读验证。

## 13. 当前已知限制

- 认证是**进程内一次性 session**：没有 SQLite、跨运行状态库或持续同步 ledger，也不存在 `auth.json` 或任何持久化凭证/身份状态；每次运行都是进程内只读，唯一可落盘的 JSON 只是导出产物。
- 没有通知读取、LLM/MCP 上下文组装或评论写接口（含确认式/幂等发送）；`is_self` 只服务于输出期展示，不构成持久事实。
- 凭证绝对不进入输出、日志、诊断、异常、对象 repr、文档或模型上下文；这是当前实现的硬约束，不是可选策略。
- 无法恢复从未观察到且已删除或屏蔽的评论。

这些只是“当前不存在”的现状描述；未来方向与实施顺序由 [roadmap.markdown](roadmap.markdown) 承担。
