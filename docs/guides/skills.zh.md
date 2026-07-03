# 编写自定义技能 { #writing-a-custom-skill }

技能是存储在 `.noeta/skills/<name>/SKILL.md` 的本地静态 LLM 工作流模板。它有 YAML frontmatter（`name`、`description`、可选的 `version` / `priority`）加上一个 Markdown 主体。任何同级文件都被捆绑为模型可以 `read` 的按需资源。

## 目录结构 { #directory-structure }

```
.noeta/skills/
  pdf-extract/
    SKILL.md              # required: frontmatter + body
    references/
      pdf-spec.md         # on-demand resource
    scripts/
      extract.py          # on-demand resource
  code-review/
    SKILL.md
```

三层合并（低 → 高优先级）：

1. **内置**技能，随 Noeta 提供
2. **全局**技能，位于 `~/.noeta/skills/`
3. **工作区**技能，位于 `<workspace>/.noeta/skills/`

在更高层具有相同 `name` 的技能覆盖更低层的。

## SKILL.md 格式 { #skillmd-format }

```markdown
---
name: pdf-extract
description: Extract text and tables from PDF files.
version: "1"
priority: 100
---

# PDF Extraction Workflow

When the user asks to extract content from a PDF:

1. Use `read` to check if the file exists.
2. Run `scripts/extract.py` via `shell_run`.
3. Summarize the extracted text.

Base directory for this skill: <absolute path>
```

### Frontmatter 字段 { #frontmatter-fields }

| 字段 | 必需 | 默认值 | 描述 |
| --- | --- | --- | --- |
| `name` | **是** | — | 稳定标识符。必须匹配 `^[a-z0-9][a-z0-9-]*$`（小写字母、数字、连字符）。用于模型的 `skill` 控制工具。 |
| `description` | **是** | — | 显示在模型看到的技能菜单中的一行摘要。 |
| `version` | 否 | `"1"` | 为 schema 演化记录；不用于过滤。 |
| `priority` | 否 | `100` | 多个技能活跃时的渲染顺序（升序）。越低 = 越先渲染。 |

未知的 frontmatter 键被容忍并存储为元数据——它们永远不会导致解析失败。因此携带额外键（如 `allowed-tools` 或 `argument-hint`）的真实公开技能可以无错误加载。

### 主体 { #body }

Markdown 主体在技能被激活时逐字追加到半稳定 View 段中。不会发生资源的急切内联——模型使用 `read` 按需拉取它们。

`Base directory for this skill:` 行由运行时自动注入，以便模型可以将相对引用（例如 `references/pdf-spec.md`）解析为绝对路径。

## 激活如何工作 { #how-activation-works }

技能以**两个阶段**激活，均由模型驱动：

### 阶段 1：菜单渲染 { #stage-1-menu-rendering }

在会话开始时，技能索引器扫描所有三层并构建一个 `name → description` 注册表。该注册表被渲染到 `skill` 控制工具的 schema 中，因此模型看到一个菜单，如：

```
Available skills:
  pdf-extract — Extract text and tables from PDF files.
  code-review — Review code for bugs and style.
```

此时不加载任何技能主体——只有名称和描述。

### 阶段 2：主体加载 { #stage-2-body-loading }

当模型使用名称调用 `skill` 控制工具时（例如 `skill: pdf-extract`），该技能的主体 + base-directory 行被 fold 到下一轮的半稳定上下文中。然后模型可以通过绝对路径 `read` 捆绑的资源。

这种两阶段设计保持初始上下文精简，同时仍然让模型按需发现和激活专门的工作流。

## 示例：一个 "research" 技能 { #example-a-research-skill }

```markdown
---
name: research
description: Search the web, read sources, synthesize findings.
version: "1"
---

# Research Workflow

Follow this process when asked to research a topic:

1. Use `web_search` to find relevant sources.
2. Use `webfetch` to read at least 3 sources.
3. Cross-reference claims across sources.
4. Write a structured summary using `write`.

## Quality bar

- Cite URLs for every claim.
- Flag conflicting information.
- Prefer primary sources over secondary.
```

将其放在工作区中的 `.noeta/skills/research/SKILL.md`。在下一次会话开始时，它自动出现在技能菜单中——无需注册步骤。

## 要点 { #key-points }

- **`SKILL.md` 是清单。** 文件名是固定的；目录名是磁盘上技能的身份（尽管 frontmatter 中的 `name` 是规范键）。
- **无热重载。** 技能在会话开始时被索引。编辑一个技能，开始一个新会话——更新后的版本会加载。
- **资源是按需的。** 同级文件永远不会被内联。模型通过 `read` 工具显式读取它们，与任何工作区文件相同。
- **技能 ≠ 工具。** 工具是模型调用的可调用函数。技能是指导*使用*哪些工具以及按什么顺序使用的 Markdown 工作流模板。见[术语表](../reference/glossary.md#skill)。

## 来源 { #source }

- 技能索引器：`packages/noeta-runtime/noeta/context/skills/indexer.py`
- Frontmatter 解析器：`packages/noeta-runtime/noeta/context/skills/_frontmatter.py`
- 另见：[ADR：模型驱动的技能调用](../adr/model-driven-skill-invocation.md)、[ADR：技能资源按需加载](../adr/skill-resource-on-demand.md)
