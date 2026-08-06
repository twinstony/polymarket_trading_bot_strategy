# AGENTS.md

- Do not preserve backward compatibility. Remove obsolete paths instead of adding compatibility layers, fallbacks, or migrations.
- Choose the simplest implementation that fully meets the current requirements. Avoid speculative abstractions, configuration, and indirection.
- Grow the system in layers. Start from the smallest version that works end to end, and add each new capability on top of a product that already works. Never trade a working product for unfinished complexity.
- Keep components modular and concerns clearly separated.
- Prefer established, well-maintained libraries when they reduce overall complexity or improve reliability. Do not reimplement common functionality without a clear reason.
- Lean on the dependencies already in the project before writing your own implementation or adding packages. Do not assume a library lacks a capability without checking its documentation and types.
- Make architectural decisions for the long term. Do not accept a stopgap that only works for now and is meant to be replaced later.

## 语言约定

- 所有与用户的对话、生成的文档（spec / PRD / CONTEXT.md / ADR / 调研报告）、issue 正文与评论，一律使用**简体中文**。
- 技术术语、GitHub issue 标签名（如 `ready-for-agent`、`needs-triage`）、代码标识符、CLI 命令保持英文原样。
- 任何 skill在本项目中执行时，默认以简体中文进行，除非用户明确切换语言。
