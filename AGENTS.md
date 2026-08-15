# AGENTS.md（编码 Agent 规则）

本文件刻意保持简短。项目有三份长期文档，均位于 `docs/`：

1. `docs/project.markdown` —— 项目为什么存在、长期意图与常青原则，以及“项目总体记忆”与 Comet 功能历史的边界。
2. `docs/architecture.markdown` —— 当前实现的结构：数据流、模型、树与对话链算法、Adapter 边界、错误与分页原则。
3. `docs/roadmap.markdown` —— 未来方向与依赖顺序；只读计划，不是实施授权。

是否阅读、何时阅读由当前 Agent 根据任务需要自行判断。

规则：

- 只实现用户明确授权或当前 Comet change 界定的范围；roadmap 不是实施授权，不得提前实现后续里程碑。
- 如果发现文档与实际冲突，先记录差异并向用户确认，不要擅自扩大范围。
- 若任务属于活跃 Comet change，按该 change 的入口读取 `docs/comet` 对应产物；不要手工修改 Comet Runtime 状态。
- 参考项目仅供思路参考，不要复制其代码或范围。

<!-- CODEGRAPH_START -->
## CodeGraph

In repositories indexed by CodeGraph (a `.codegraph/` directory exists at the repo root), reach for it BEFORE grep/find or reading files when you need to understand or locate code:

- **MCP tool** (when available): `codegraph_explore` answers most code questions in one call — the relevant symbols' verbatim source plus the call paths between them, including dynamic-dispatch hops grep can't follow. Name a file or symbol in the query to read its current line-numbered source. If it's listed but deferred, load it by name via tool search.
- **Shell** (always works): `codegraph explore "<symbol names or question>"` prints the same output.

If there is no `.codegraph/` directory, skip CodeGraph entirely — indexing is the user's decision.
<!-- CODEGRAPH_END -->

<comet-ambient-resume>
<!-- Managed by Comet. Edits inside this block may be replaced by comet init/update. -->
<!-- Contract: comet.resume_probe.v2 -->

## Comet Ambient Resume

在这个仓库中，开始处理需要改动或调查的任务前，如果可能存在活跃 Comet workflow，把当前用户请求传入只读探针：`comet resume-probe . --stdin --json`。

- 如果用户通过宿主明确调用任意 Comet Skill（例如 `@comet`、`/comet`、`@comet-native` 或 `/comet-hotfix`），显式调用优先于本恢复协议；不要运行 resume probe，直接进入被调用的 Skill。
- 只信任返回的 `workflow`、`skill` 和 `entrySource`；它们只由项目配置或无配置兼容回退决定。不得扫描或切换另一套 workflow。
- 如果 probe 返回 `auto_resume`，简短说明选中的 active change，并进入 `nextCommand` 指向的永久入口。不要把状态命令当作恢复入口直接推进。
- 如果 probe 返回 `ask_user`，只问一个简短问题并等待用户回复。
- 如果当前请求未明确调用 Comet Skill，且 probe 返回 `out_of_scope` 或 `none`，不要进入 Comet workflow。
- 如果配置或状态无效且没有 `nextCommand`，停止并报告原因；不要猜测另一个 workflow。
- 不能只因为存在 active change 就把无关任务挂到该 change。Native 的未提交改动由 Native 入口检查，不由探针自动归因。
</comet-ambient-resume>
