# Contributing to octop-browser

Thank you for your interest in contributing! This guide applies to the [OrcaKit Harness](https://github.com/TencentCloud) / [Octop](https://github.com/TencentCloud/Octop) ecosystem.

## Getting started

**Prerequisites:** Python 3.11+, Chromium (optional — integration tests skip when unavailable), [uv](https://docs.astral.sh/uv/)

```bash
git clone https://github.com/TencentCloud/octop-browser.git
cd octop-browser
make install        # install dev dependencies
make all            # lint + typecheck + test (CI ship bar)
```

## Development workflow

| Command | Description |
|---------|-------------|
| `make install` | Install dev dependencies |
| `make all` | Lint + typecheck + test (required before PR) |
| `make lint` | Ruff check + format check |
| `make format` | Auto-fix and format code |
| `make typecheck` | mypy strict |
| `make test` | pytest with coverage |
| `make run` | Run octop-browser module |
| `make mcp` | Start MCP server |
| `make build` | Build wheel + sdist |

## Branching

| Branch | Role |
|--------|------|
| `main` | Production source of truth; default branch; daily integration |
| `feature/*` / `fix/*` | Feature and fix branches — open PRs against `main` |
| `release/x.y.z` | Temporary release snapshot; deleted after the version ships |
| `hotfix/*` | Emergency patch from `main`; merge to `main` then tag |

```
feature/* ──PR──► main
release/x.y.z ──PR──► main ──auto-tag v*──► PyPI + GitHub Release
hotfix/* ──PR──► main ──auto-tag v*──► publish
```

**Rules:**

- Open feature / fix PRs against **`main`** with a `feature/` or `fix/` branch prefix.
- Ship releases only via `release/x.y.z` → `main` (or `hotfix/*` → `main`). Do **not** push production `v*` tags from a feature branch.
- Merge `release/x.y.z` → `main` with a **merge commit** when possible (keeps tag ancestry clear).
- Production `v*` tags are created **on `main` after** the release PR merges — not on the release branch before merge.
- After publish, delete the temporary `release/x.y.z` branch.

## Pull requests

1. Fork (if needed) and create a feature branch from **`main`**
2. Add or update tests for behavior changes
3. Run `make all` locally — CI must pass
4. Update `CHANGELOG.md` under **Unreleased** when user-facing behavior changes
5. Open a PR with a clear description and test plan

## Code style

- **Ruff** for linting and formatting (`line-length = 88`)
- **mypy** strict mode for type checking
- Async-first for all browser I/O
- Unit tests run without Chrome; integration tests skip when Chromium is missing

## Releases

1. Cut `release/x.y.z` from latest `main` (version bump + CHANGELOG on that branch)
2. Open PR: `release/x.y.z` → `main` and merge when green
3. GitHub Action `Auto Tag On Release Merge` reads `pyproject.toml` on **main tip**, pushes `v<version>`, and dispatches `Release`
4. `Release` builds, publishes `octop-browser` to PyPI (trusted publishing), and creates the GitHub Release
5. Delete `release/x.y.z`

Agent-assisted publish: `.cursor/skills/publish` / `.codebuddy/skills/publish` (`/publish <version>`).

Manual fallback: push a `v<version>` tag on main tip yourself (must match `[project].version`).

### Hotfix

Branch from `main` → PR into `main` (tag via the same auto-tag path if shipping a patch).

---

# 贡献指南

感谢你对 octop-browser 的关注！本指南适用于 [OrcaKit Harness](https://github.com/TencentCloud) / [Octop](https://github.com/TencentCloud/Octop) 生态。

## 环境搭建

**前置条件：** Python 3.11+；Chromium（可选，未安装时集成测试自动跳过）、[uv](https://docs.astral.sh/uv/)

```bash
git clone https://github.com/TencentCloud/octop-browser.git
cd octop-browser
make install
make all            # 提交 PR 前必须通过
```

## 分支策略

| 分支 | 角色 |
|------|------|
| `main` | 生产真源；默认分支；日常集成 |
| `feature/*` / `fix/*` | 特性 / 修复分支 — PR 打向 `main` |
| `release/x.y.z` | 临时发版分支；发版完成后删除 |
| `hotfix/*` | 从 `main` 紧急修复；合入 `main` 后自动打 tag |

**规则：**

- 特性 / 修复 PR 请打向 **`main`**，分支名使用 `feature/` 或 `fix/` 前缀。
- 发版必须走 `release/x.y.z` → `main`（或 hotfix → `main`）。禁止在 feature 分支上推送生产 `v*` tag。
- `release/x.y.z` → `main` 尽量使用 **merge commit**。
- 生产 `v*` tag 仅在 release PR **合入 `main` 之后**打在 main tip 上。
- 发版后删除临时 `release/x.y.z` 分支。

## 提交流程

1. 从 **`main`** 创建特性分支
2. 为行为变更补充测试
3. 本地运行 `make all`
4. 用户可见变更请更新 `CHANGELOG.md` 的 **Unreleased**
5. 提交 Pull Request

## 代码规范

- Ruff lint + format（`line-length = 88`）
- mypy strict 类型检查
- 浏览器 I/O 优先异步
- 单元测试无需 Chrome；未安装 Chromium 时集成测试自动跳过

## 发版

1. 从最新 `main` 切 `release/x.y.z`（在该分支 bump 版本与 CHANGELOG）
2. PR：`release/x.y.z` → `main`，合并通过后
3. Action `Auto Tag On Release Merge` 读取 main tip 的 `pyproject.toml`，推送 `v<version>` 并触发 `Release`
4. `Release` 构建、发布 `octop-browser` 到 PyPI，并创建 GitHub Release
5. 删除 `release/x.y.z`

Agent 辅助发布：`.cursor/skills/publish` / `.codebuddy/skills/publish`（`/publish <version>`）。

手动兜底：在 main tip 自行推送与 `[project].version` 一致的 `v<version>` 标签。
