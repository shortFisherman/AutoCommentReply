# 本地认证与 viewer 身份

## 能力目标

系统在一次只读讨论同步中必须明确知道它是以 anonymous 视角还是某个已认证 Bilibili viewer 视角运行。viewer 的稳定身份是 `platform_user_id`（B 站 mid）；username 仅用于展示。认证材料保留在本机 session 边界内，永不成为输出或模型可见数据。

## 认证输入与 session

- 没有认证输入时，系统创建 anonymous session；`authenticated=false`，`platform_user_id=null`，`username=null`。
- 有认证输入时，系统创建 authenticated session，将凭证仅注入允许 Bilibili 主机的 HTTP Cookie header，并在读取评论前确认当前 viewer。
- viewer 通过 `GET https://api.bilibili.com/x/web-interface/nav` 解析。登录响应必须明确表示已登录，并提供可解析为正整数的 mid；username 可空且仅展示。
- 同一 Adapter 生命周期内 nav 响应缓存并复用；legacy WBI 密钥读取如同时需要 nav，必须共享该响应，不能重复身份请求。
- 提供了凭证但 nav 表示未登录、mid 缺失/非法或结构无效时必须 fail closed，不得按 anonymous 继续。
- 认证输入只沿用现有 `--cookie-file` 与 `BILIBILI_COOKIE` 环境变量；`--cookie-file` 保持优先。系统不新增 `auth.json`、`--auth-file`、默认凭证路径或跨运行认证状态，也不得新增 argv 明文 Cookie。
- Cookie 输入与解析出的 viewer 在进程内组成同一 session 边界；session 生命周期不超过 Adapter 生命周期，关闭 Adapter 时同步结束。

## Viewer 模型

viewer 输出是无凭证事实对象：

```json
{
  "platform": "bilibili",
  "authenticated": true,
  "platform_user_id": 123456,
  "username": "display-only"
}
```

anonymous viewer 的 `platform_user_id` 与 `username` 均为 JSON `null`。username 不参与相等性、discussion identity、同步范围或任何授权判断。

## 评论作者与 `is_self`

- 评论作者稳定身份使用 `author_id`。M2 定向 schema 只输出 `author_id`，不输出 `user_id` 兼容别名；legacy schema 1.0 继续使用原有 `user_id`。
- `is_self` 不存入平台中立 Comment 事实模型；输出层对平面 comments 与嵌套 trees 统一派生。
- viewer 已认证且作者身份已知时：`author_id == viewer.platform_user_id` 为 `true`，否则为 `false`。
- viewer anonymous 或作者身份未知时，`is_self=null`；不得用 `false` 伪装未知。

## 输出与兼容

- 定向讨论输出从 schema 1.1 升级为 schema 1.2，新增顶层 `viewer`，并将评论及树节点的作者字段从 `user_id` 替换为 `author_id`；不双发兼容别名。依赖 schema 1.1 的消费者必须按版本显式迁移。
- legacy 整视频诊断输出继续使用 schema 1.0，字段与现有行为不变。
- discussion identity 继续是 `(platform, object_type, oid, root_comment_id)`，不包含 viewer 或 focus。
- viewer 只改变 viewer-relative `is_self` 和平台可见范围；同一输入的目标根、focus 规则、建树规则与请求分页范围不变。

## 请求与错误语义

- anonymous 定向同步不为身份识别请求 nav。
- authenticated 定向同步每个 Adapter 生命周期至多增加一次 nav 身份请求；评论读取本身仍不调用主评论 `main`、不做 WBI 签名，`root_pages_fetched=0`。
- 认证身份无法确认发生在评论读取前，作为读取前致命错误返回 CLI exit 1，不产生 discussion JSON。
- 评论读取期间现有 complete、diagnostics 与 exit 0/2 语义保持不变。

## 凭证安全

- 凭证不得出现在 stdout、输出文件、stderr/verbose 日志、异常消息、diagnostics/details、对象 repr、文档、fixture、Runtime handoff、Verifier 报告或模型上下文。
- 不允许将 Cookie 放入 URL/query、命令行明文参数或可提交文件。
- 错误处理只输出固定的脱敏身份/鉴权说明，不回显请求 headers、Cookie、认证文件内容或服务端可能包含敏感请求材料的 payload。
- 自动测试必须使用明显唯一但完全虚假的 secret，并递归断言所有可观察输出都不含该 secret。

## 明确不包含

本能力不读取通知、不创建 SQLite/ledger、不计算跨运行可见性 diff、不实现写接口、登录 UI、扫码登录、浏览器自动化、自动刷新凭证、系统 keyring 或 LLM/MCP 上下文。
