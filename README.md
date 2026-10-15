# 开发会话指挥室

## 多 Agent 工作台

新增统一入口 `/workspace`：项目与待办概览、可持久化的任务、Agent 能力与会话入口、Codex 官方订阅额度、人工每日用量记录、固定来源的 AI 资讯。架构选择、开源参考和后续执行控制路线见 [架构方案](docs/current/agent-workbench-20261013/hlplan.md)，本轮证据与限制见 [实施报告](docs/current/agent-workbench-20261013/impl_report_r1.md)。

启动下述新版网关后打开 `http://127.0.0.1:8766/workspace`，或执行 `& './scripts/open-workspace.ps1'` 进入 Edge 应用窗口。脚本只打开已运行的服务，不替换占用端口的旧进程。本轮为保留原 8766 服务，另在 `http://127.0.0.1:8770/workspace` 运行预览，可执行 `& './scripts/open-workspace.ps1' -Port 8770` 打开。

新增任务与人工记录保存在项目根目录 `workspace-state.sqlite3`，原有项目注册、待办和手工进度继续使用既有文件。首次没有任务时显示空状态；任务负责人表示人工分配记录，不会向 Agent 发送命令。每日 Token/费用与 Grok 额度目前需要手工记录；Codex 额度只在点击查询后通过官方 app-server 读取。资讯为固定 RSS 抓取，含来源与采样时间，不是推送流。

桌面浏览器及 360/390px 布局已有实际验收；当前仅监听本机。真正手机远程访问、Agent 派发/审批/取消、自动每日成本、原生安装包属于后续里程碑。Edge 入口已运行验证，2026-10-13用户已目检确认独立窗口正常显示；该观察不代表原生安装包或手机远程能力已经交付。

下文保留原会话与协作小院的使用说明。

在一个页面查看本机 Codex 身份与远端 Claude 对话，用真实 UUID 对应机器人。远端消息与 Zellij 定位沿原有证据核验；三个只读 MCP 工具仍仅查询远端 Claude 索引。

当前首批固定来源为 `example@192.0.2.10`、`/home/example/.claude`、Claude Code `2.1.278`；本机 Zellij `0.45.1` 的 `workbench-v4 / Work-for-Linux`。元数据、存活进程和终端位置分别验证。此列表不是全服务器会话总数，也不判断任务是否完成。

## 前提与两种运行环境

本机 Python 3.10+、已有 SSH known_hosts/认证与 Terminal Workbench。观察不要求重启 Claude 或安装 Hooks；SSH 使用严格主机检查与非交互认证。

本机专属 `.venv` 只安装 `requirements.txt` 中的 MCP `1.26.0`。远端独立 reader 环境为 `/home/example/.local/share/session-panel-reader/.venv`，安装 `requirements-reader.txt` 中的 Claude Agent SDK `0.2.163` 与 MCP `1.26.0`。安装与真实兼容由协调者核验；不使用系统 pip，不改现有 CLI/PATH/Claude 设置。远端环境缺失时仍可观察身份，消息显示 `sdk_unavailable`。

```powershell
Set-Location 'C:/example/control-everything'
& 'C:/example/private-8e9f974a.exe' -m venv .venv
& '.venv/Scripts/python.exe' -m pip install -r requirements.txt
& 'C:/example/private-b8446a21.exe' -m session_panel.gateway
```

环境已存在时先核对归属和版本，不覆盖现有环境。生产网关固定监听 `http://127.0.0.1:8766/`；端口占用会退出，不换端口或接管现有服务。默认前台手动运行，Ctrl+C 停止；不配置自动启动。

## 网页使用与状态

打开 `http://127.0.0.1:8766/`。远端 Claude 列表按 UUID 分行，即使两个会话 cwd 相同。点击明确的远端 ID 才读取最近 20 条可见消息；刷新观察只更新元数据。刷新最近消息与翻页才再次读取正文，隐藏页面暂停 10 秒一次的元数据轮询。

✅ 已实现：本机 Codex 只读身份索引，固定读取 `C:/example/private-69202ba3.sqlite` 的六个元数据字段，筛选未归档 cli/exec/vscode，每页 12 条；归档、子代理及其他来源数量另列。角色键使用 `codex:<UUID>`，与远端相同 UUID 仍是两个身份。选择 Codex 只显示身份详情、清空远端正文并禁用远端定位；失败或超过 30 秒的采样清空当前角色。内部格式变更时明确显示不可用。

✅ 已实现：3D 工位、每个可见身份的独立区域、圆角巡游、平滑起停/归位/巡游选中收腿、不同巡游节奏、光粒子、远端会话证据流程、环绕/缩放、暂停与像素/文字回退。在场 UUID 刷新保持位置，翻页重新安排；原会话观察区尚未提供永久座位或实际 session 协作。页面显示近期可见帧的 FPS/p95；巡游与动态连线不代表工作状态或任务进度。运动和个人区域验收见 [本批报告](docs/current/robot-personal-space-delivery-r1.md)。

⛔ 未实现/未验收：Codex 正文与实时活动监控；全服务器会话覆盖；真实 WezTerm 正向关联和定位。本轮实机观察 workbench-v4 已退出，远端 terminal_unavailable，不能以本地测试推断双端会话全部接入。当前证据与失败记录见 [本轮交付报告](docs/current/game-scene-delivery-r1.md)。

身份 `verified`、进程 `observed_running`、位置 `confirmed` 是独立证据；活动和项目完成始终 `unknown`。断连或来源失败立即显示未知，并保留“上次证据”；超过 30 秒的证据不作当前状态。定位需要 5 秒内的唯一实例和当前 pane，点击后重新核验，只切换已有 Zellij pane，不输入字符或激活桌面窗口。命令发送后还须唯一客户端焦点和 pane 清单确认，否则显示 `focus_sent_unverified`。

WezTerm 状态区单独观察固定 `workbench-v4` 后台、GUI 进程、交互客户端与证据年龄；来源绿点仅表示远端采样。只有后台存活、未关联且证据足够时，才能显式“连接现有工作台”。已有连接不会重复创建；已退出或不存在的后台不会复活或新建。外部独立 mux 仅计数，不采用或关闭。GUI 可见性和 watcher 覆盖未核验，成功只称“唯一关联观察”，不是 client-PID 握手或原子实例绑定。

选择 UUID 后，定位还要求 5 秒内的唯一工作台关联，动作前后重新检查原实例、客户端和 pane。显示“已确认 Zellij 选中 pane”后，请切到 WezTerm 查看。刷新终端状态或连接结果不会重新读取消息。若出现 `cleanup_unverified`，页面禁用连接与定位，请核查本次新进程的回收状态；不提供自动重启或解除入口。

消息使用官方 reader 的六字段，单条消息时间省略；“本页读取时间”只表示读取时点。`reader_scope_unverified` 表示目录与 reader 查找范围不能证明一致；`reader_empty_unverified` 表示 reader 返回空结果，真正空历史也未验证。它们不会显示为“暂无对话/读取成功”。文件变化返回 `messages_changed` 或 `revision_mismatch`，请刷新最近消息。文件上限 16 MiB、读取 20 秒、响应 512 KiB、每页 1–50 条；超预算不会静默截断。

正文只留在请求/页面内存，不写磁盘、浏览器存储或研究日志；不默认加载消息图片/附件，不发给 Jev 或其它外部模型。页面错误保留当前选择和上次消息结果。

## Codex MCP 接入

先保持网关运行。`.codex/config.toml.example` 是可审阅的示例，协调者只合并该 server 表到受信任项目的 `.codex/config.toml`，不覆盖其它服务器或写全局配置。

```toml
[mcp_servers.remote_claude_sessions]
command = "C:/example/private-b8446a21.exe"
args = ["-m", "session_panel.mcp_stdio"]
cwd = "C:/example/control-everything"
enabled_tools = ["list_sessions", "get_session_status", "get_session_messages"]
```

工具仅有 `list_sessions()`、`get_session_status(session_id)`、`get_session_messages(session_id, offset=None, limit=20, revision=None)`；没有 focus、发送、审批、resume 或终止工具。stdio stdout 只发送 MCP 协议，网关未运行会明确返回 `gateway_unavailable`，不会自动再建索引。

保存配置不代表当前聊天已加载。依照 [Codex 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=app)由用户控制客户端正常重载；不静默重启当前 Codex。`codex mcp list` 只证明配置，真正接入还需加载后发现恰好三个工具，并按两个真实 ID 调用，与网页结果核对。内部 `8766` HTTP 地址不是 MCP URL。

## 同主会话子agent协作小院

网关由当前主 Codex 会话的 `CODEX_THREAD_ID` 启动且能够核验本机原生父子元数据时，首页默认进入“协作小院”。小院只列出这个主会话明确登记的子agent；点击“会话与终端观察”可回到原页面。相同目录或相同工作环境不会合并不同主会话。页面中的草木色庭院和像素机器人是可选择的角色场景，动画不表示任务正在运行。

当前闭环支持四种身份：主会话协调者登记子agent、分配固定产物、请求复核和验收；提交者用自己的会话身份交付；另一位已登记复核者用自己的身份接收复核并记录结论。每一步产生收据，产物与复核证据按版本和 SHA-256 核验。只有当前版本经过独立复核且由主会话验收后，云朵才显示“完成交付”的短摘要；展开工位可见完整身份、路径、哈希和历史收据。文件变更会使当前结果失效，重新交付会产生新版本。

主屏的伙伴名牌先显示登记角色、负责事项和已有收据阶段；点选名牌或场景角色后，工位展示职责、能力、实际工作节点与时间。公开发言按需从当前主会话已登记子agent的本机记录中读取最近最多六条 assistant 公开发言；这是有限摘录，不是完整聊天，也不用于推断实时运行进度。小院默认自主巡游，动作按钮在小院隐藏；3D、像素、文字模式仍可选择，原“会话与终端观察”保留原控制。页面详情与使用边界见 [清晰主界面与角色详情](docs/current/subagent-garden-detail/README.md)。

本地 MCP 服务是 `session_panel.garden_mcp`，提供 `garden_context`、`garden_register_actor`、`garden_assign_task`、`garden_submit_artifact`、`garden_request_review`、`garden_claim_review`、`garden_record_review`、`garden_accept_artifact` 八个固定工具。各子agent从**自己的**进程执行固定客户端，客户端继承其 `CODEX_THREAD_ID`，不接受参数冒充身份：

```powershell
& '.venv/Scripts/python.exe' -B -m session_panel.garden_invoke --list
& '.venv/Scripts/python.exe' -B -m session_panel.garden_invoke --tool garden_context --arguments-file work/garden-context-args.json
```

其中 `work/garden-context-args.json` 的内容是 `{}`；其他工具的参数字段以 `--list` 返回的 schema 为准。参数文件必须是本项目内的相对路径。主会话发出请求后，仍须由协调者把复核收据交给复核者；“已请求”不等于对方已接收。工具是实际可调用的本地 stdio 接口，本次已经用不同真实子agent进程走通交付、复核和验收；当前 Codex 桌面客户端是否自动发现这个新服务尚未验证，不把它称为已自动接入。

协作记录保存在项目内 `work/garden-state/<主会话 UUID>.sqlite`，按根 Thread UUID 分域；客户端不能通过 HTTP 参数切换根身份。该边界依赖本机受信任的进程环境和原生元数据，适用于本机正常协作，不承诺抵御同一系统账户故意篡改环境或数据库。完整设计、测试和真实操作证据见 [子agent协作小院](docs/current/subagent-garden/README.md)。

## 验证与撤销

本地自证只用合成 fixture、mock SSH/Zellij、临时 loopback 与 MCP 协议：

```powershell
& '.venv/Scripts/python.exe' -m unittest discover -s tests -v
& '.venv/Scripts/python.exe' -m compileall session_panel
& 'C:/example/private-318dad85.exe' work/verify-ui-r1.cjs
& 'C:/example/private-318dad85.exe' tests/verify_robot_cockpit.cjs --fixture
```

真实 SSH 补绑、独立 SDK 合成链/空结果对照、选中真实 UUID 分页、Edge 桌面/360px、真实 focus 和 Codex 加载由协调者分别承接。fixture 自证不代表这些已验收；详细状态与所有失败见 `docs/current/unified-dev-control-panel/impl_report_r1.md` 及 `work/` 证据。

Ctrl+C 停网关即可停本批查询，原 Claude/SSH/Zellij 对话继续运行。撤销只移除本批新增源码/web/tests/requirements/根 README/example；真实 MCP 回退仅撤销本 server 表并由用户重载。删除本地 `.venv` 或远端 reader `.venv` 前确认绝对路径和本批归属。保留研究、方案和实施报告；不删除远端 `.claude`、项目或 Workbench。已经切换的 pane 由用户自行切回。
