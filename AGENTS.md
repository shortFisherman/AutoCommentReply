# AGENTS.md（编码 Agent 规则）

本文件刻意保持简短。所有编码 Agent 在开始任何工作前，必须先完整阅读：

1. `docs/PROJECT.md` —— 做什么、不做什么、验收标准。
2. `docs/ARCHITECTURE.md` —— 数据流、模型、树与对话链算法、Adapter 边界、错误与分页原则。
3. `docs/DECISIONS.md` —— 关键决定与原因，避免无意中推翻既定决定。

规则：

- **只实现当前里程碑**（现在是 MVP1：完整评论树读取），不要提前实现 AI、数据库、自动回复、前端等后续功能。
- 如果发现文档与实际冲突，先记录差异并向用户确认，不要擅自扩大范围。
- 参考项目仅供思路参考，不要复制其代码或范围。

<!-- CODEGRAPH_START -->
## CodeGraph

In repositories indexed by CodeGraph (a `.codegraph/` directory exists at the repo root), reach for it BEFORE grep/find or reading files when you need to understand or locate code:

- **MCP tool** (when available): `codegraph_explore` answers most code questions in one call — the relevant symbols' verbatim source plus the call paths between them, including dynamic-dispatch hops grep can't follow. Name a file or symbol in the query to read its current line-numbered source. If it's listed but deferred, load it by name via tool search.
- **Shell** (always works): `codegraph explore "<symbol names or question>"` prints the same output.

If there is no `.codegraph/` directory, skip CodeGraph entirely — indexing is the user's decision.
<!-- CODEGRAPH_END -->
