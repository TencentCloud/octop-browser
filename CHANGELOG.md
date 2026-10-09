# Changelog

本文件记录 `octop-browser` 的版本变更。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 修复

- 启动 Chrome 不再改写进程级 `XDG_RUNTIME_DIR`：`ensure_xdg_runtime_dir(env)` 与 `prepare_chrome_launch()` 会把它无条件写入 `os.environ`，而该变量是进程全局状态——宿主进程里其他子进程（如 `systemctl --user`，user bus 位于 `$XDG_RUNTIME_DIR/bus`）随后都会解析到错误的 socket，导致 Octop Dashboard「更新后重启服务」失败（TencentCloud/Octop#1833）。现仅在传入的子进程 env 中注入，`os.environ` 保持不变；需要显式改写进程环境的调用方仍可不传 env。
- Linux arm64 上无法识别 Playwright 已下载的 Chromium：Playwright 没有官方 Linux arm64 构建，会下载非官方回退包（日志中的 `downloading fallback build for ubuntu24.04-arm64`），其平台目录为 `chrome-linux-arm64/`，而 `_playwright_exe_candidates` 只探测 `chrome-linux64/` 与 `chrome-linux/`，导致 `find_chrome()` 返回 `None`、`verify_chromium()` 报 “Chromium binary not found”，安装流程反复重下（二进制其实已完整落盘且可运行）。现补入 `chrome-linux-arm64/`，排在 `chrome-linux64/` 之后、`chrome-linux/` 之前。
- 新增按目录名匹配的兜底探测（Linux `chrome-linux*`、Windows `chrome-win*`、macOS `chrome-mac*`）：Playwright 的目录名随其实际下载的构建而变，未硬编码的新布局也能被发现。

### 变更

- 对齐 Octop：引入 `develop` 集成分支策略；禁止直推 `main`/`develop`；发版后由 `sync-main-to-develop.yml` 同步；新增 `/publish` skill（发版同步 CHANGELOG / README）。


## [1.0.0] - 2026-09-24

`octop-browser` 的首个正式版本：一个面向 Agent 的浏览器自动化工具，直连
**Chrome DevTools Protocol（CDP）**，不经 Playwright 中间层，因此元素定位基于
实时 DOM 引用（ref），点击更准、token 开销更低、登录态可长期保持。

### 核心能力

**CDP 直连架构**
- 启动真实 Chromium 并直接通过 CDP 通信，去掉中间层，动作语义与浏览器行为一致。
- 按 profile 复用浏览器进程与端口，跨命令自动重新接入同一标签页。

**DOM 四档粒度**
- `minimal` / `interactive` / `structured` / `full` 四个级别，按需取用。
- Agent 常用 `interactive` 只取可交互元素并返回稳定 ref，显著降低 token 消耗；
  `structured` 输出 JSON 供脚本消费。

**浏览器动作**
- 导航：`navigate` · `go_back` · `go_forward` · `reload`
- 交互：`click` · `type` · `fill` · `select` · `hover` · `press` · `scroll`
- 观察：`dom_tree` · `screenshot` · `eval_js` · `wait`
- 标签页：`new_tab` · `switch_tab` · `close_tab` · `list_tabs`
- 会话：`close_session`（默认仅断开连接，`kill=True` 同时结束浏览器进程）
- 元素既可用 ref 定位，也可用 CSS selector 定位。

**登录态持久化**
- 命名 profile 各自拥有独立的 Chrome user-data-dir，登录一次即可长期复用。
- profile 与 CDP 端口的映射持久化在磁盘，进程重启后仍能对应。

**录制与回放**
- `record start/stop` 录制真实浏览过程，自动把原始事件归纳为语义化步骤。
- `record generate-steps` 导出可复用步骤，`record generate-skill` 生成 Agent Skill 草稿。
- `replay run` 回放录制，支持 `--input` 注入参数实现参数化重放。
- 三档隐私模式（`--privacy`）：默认对密码、token、密钥等敏感字段自动脱敏，
  录制产物中不落明文；事件文件以 `600` 权限写入。

**MCP 服务**
- 通过 stdio 提供 MCP 协议，兼容主流 Agent 客户端。
- 每个动作对应一个独立工具（`browser_navigate`、`browser_click`、
  `browser_dom_tree`、`browser_close_session` 等），参数与 Python API 一致。

**CLI 与 Python API**
- `octop-browser` 命令覆盖全部动作，适合脚本与手工调试。
- Python 侧提供 `BrowserSession`（持久会话）与 `browser_tool`（单次无状态调用）
  两种入口，统一返回 `ToolResult`（`success` / `content` / `error` / `metrics` / `metadata`）。
- `OctopSettings` 集中管理配置，17 个 `BROWSER_USE_*` 环境变量均可覆盖默认值。
- 钩子机制（`before_action` / `after_action` / `action_error`）便于接入日志与观测。

**浏览器自动安装**
- `install-browser` 自动下载并校验受管 Chromium，无需预装浏览器。
- 提供流式安装事件，便于外部展示进度。

[Unreleased]: https://github.com/TencentCloud/octop-browser/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/TencentCloud/octop-browser/releases/tag/v1.0.0
