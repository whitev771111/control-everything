# 开发会话指挥室

## 面板演示



1080p 中文配音与字幕，展示项目任务、会话观察、用量来源和协作收据。画面使用 `44c359f` 版本的真实界面与演示数据；已实现与待验收能力分别说明。

当前项目与进度兼容增量见[实现与验收状态](docs/current/project-progress-compat-20261014/README.md)；较早里程碑见[开发进度](docs/DEVELOPMENT_STATUS.md)。默认启动使用当前用户独立的空 profile，通过页面登记项目，不再要求复制个人 registry 文件。已有本机文件保留，可显式导入。

## 源码与测试目录

后端保留 `session_panel` 包，内部按 `http`、`sessions`、`workspace`、`execution`、`garden`、`profiles`、`integrations` 七个功能包组织。前端使用原生 HTML/CSS/JS，页面资源放在 `web/sessions`、`web/progress`、`web/workspace`、`web/garden`，无需构建步骤。公开页面和资源 URL 保持不变，例如 `/workspace.js` 对应物理文件 `web/workspace/workspace.js`。

测试按 `tests/backend`（Python）、`tests/frontend`（纯 Node 合同）、`tests/e2e`（浏览器/协议 harness）和 `tests/fixtures`（合成替身）归类。新增功能放入所属功能目录，测试按类型放置；详细职责与验证命令见 [参与开发](CONTRIBUTING.md)。原有 `python -m session_panel.gateway` 等外部 CLI 继续使用，内部代码直接导入功能包实现。

## 多 Agent 工作台

统一入口 `/workspace` 提供项目登记、多个工作目录、人工进度与历史、任务和待办、可选文档进度来源、Codex 官方订阅额度、人工每日用量和固定来源 AI 资讯。项目不要求 Git 或特定工作流。人工进度、任务统计、文档观察分别展示；未填写百分比与真实 0 不混用，文档 PASS 或来源报告完成不会改写人工状态。

在自己的检出目录使用 Python 3.10+ 创建环境；已有 `.venv` 时核对归属与版本，不覆盖：

```powershell
python -m venv .venv
& '.venv/Scripts/python.exe' -m pip install -r requirements.txt
& '.venv/Scripts/python.exe' -m session_panel.gateway
```

打开 `http://127.0.0.1:8766/workspace`。默认数据目录在 Windows 为 `%LOCALAPPDATA%/SessionPanel`（环境变量缺失时为当前用户 `AppData/Local/SessionPanel`）；目录内的 `profile.json` 保存来源设置，`workspace-state.sqlite3` 保存管理记录。路径优先级为 `--data-dir`、非空 `SESSION_PANEL_DATA_DIR`、当前用户默认目录；显式路径必须绝对。

保留已有服务时明确选择另一个端口，例如：

```powershell
& '.venv/Scripts/python.exe' -m session_panel.gateway --data-dir 'C:/example/SessionPanel/main' --port 14401
& './scripts/open-workspace.ps1' -Port 14401
```

随后访问 `http://127.0.0.1:14401/workspace` 或 `/progress`。网关只监听 `127.0.0.1`，端口范围为 1024–65535；占用即退出，不自动换端口、终止或接管旧服务。脚本只打开已经运行的服务。Ctrl+C 停止自己的实例；没有新增自动启动。

在“添加项目”填写名称与真实的主目录，可添加最多七个附加目录；搬迁时编辑路径，项目 ID 和绑定 ID 保持。归档保留任务、人工记录及待办，随时可恢复。新增任务只能选择活动项目；已有归档或未登记项目的任务可以保留原项目继续编辑，兼容记录区也可按原 ID 恢复登记。任务和用量列表提供分页，不以首批展示条数代表全集。

项目进度来源可选“未配置（人工管理）”、workflow-for-coding、普通文本或版本化 JSON。文本/JSON 使用选定目录内的相对路径；预设读取该目录的 `docs/current`，仅读有限直接子目录及 README/progress 文本。缺文件、空内容、出界或读取错误分别显示，不影响人工管理。JSON 示例：

```json
{"version":1,"state":"active","stage":"implementation","percent":0,"note":"等待测试","updated_at":"2026-10-15T08:00:00Z"}
```

除 version 外字段可选，但 state/stage/percent/note 至少一项有效。state 为 planned/active/blocked/completed；percent 为整数 0–100 或 NULL。单文件最多 256 KiB，预设每项目最多 64 个直接子目录、128 个候选文件；超预算明确报错，不把截断读取当作完整来源。

“来源与导入”可显式填写 Codex 元数据库的绝对路径并启用。只读六字段元数据，不读会话正文；禁用后不访问该库，也不回退开发者个人路径。新 profile 不装配远端 SSH、个人启动器、小院或执行 owner。额度缓存、用户点击后的官方只读额度查询和公开资讯仍保留；每日 Token/费用与 Grok 额度仍可人工记录。执行、审批、模型费用试跑及 Orca 接入沿用原有暂停边界。

## 导入旧记录与恢复

在空 profile 的“来源与导入”填写旧数据目录的绝对路径，例如 `C:/example/previous-panel`。固定读取 `progress-registry.json`，以及可选的 `progress-manual.json`、`progress-todos.json`、`workspace-state.sqlite3`。只导入全部合法项目、人工进度、待办、工作台任务和人工用量；保留原 ID、顺序、关联与已有时间。未登记项目的记录进入兼容区；没有原时间的导入快照显示“来源未提供时间”。

导入不改源目录、不读或复制 execution/审批/事件/garden/私人正文，不导入个人来源设置。非法、重复、schema 不兼容或超预算的批次整批拒绝并给出字段定位；不会只取旧页面前 500/100 条。目的 profile 必须业务为空且未导入过，来源与目的目录不能重合或互相包含。已有记录应另选空 `--data-dir`，不能覆盖或合并导入。

提交前会在目的 `backups/<UTC>-<random>/` 生成管理库、配置快照和 manifest。成功页面显示完整 counts、备份路径及排除范围；备份失败不提交导入，失败的 `.pending-*` 目录可能留作诊断。恢复在离线命令中写到另一个不存在或真正空的目录，拒绝覆盖已有 profile：

```powershell
& '.venv/Scripts/python.exe' -m session_panel.profile restore --backup 'C:/example/SessionPanel/main/backups/<备份目录>' --data-dir 'C:/example/SessionPanel/restored'
& '.venv/Scripts/python.exe' -m session_panel.gateway --data-dir 'C:/example/SessionPanel/restored' --port 14401
```

恢复会校验 manifest、文件大小/哈希、schema 与 SQLite 完整性。停止自己的旧实例后另行启动恢复目录；不在线切换，不删除原 profile 或导入后的新记录。真实个人数据是否符合导入合同需实际校验，隔离 fixture 验证不能替代个人迁移结果。

新记录保存在所选 profile，源码旁旧 JSON/SQLite 与既有服务保留原位。任务负责人表示人工分配，不向 Agent 发送命令。资讯含来源与采样时间，不是推送流。

2026-10-13 的桌面、360/390px 和 Edge 窗口验收属于旧工作台对象，证据见[旧实施报告](docs/current/agent-workbench-20261013/impl_report_r1.md)。本兼容增量的当前验证状态以[当前 feature](docs/current/project-progress-compat-20261014/README.md)为准。手机远程访问、自动每日成本和原生安装包仍是后续能力。

## 旧个人服务与协作小院（历史入口）

下文保留原版本或已有旧实例的说明。新版 profile 默认不启用这些个人服务，不能把新网关的 `/` 或 MCP 配置当作已恢复远端会话/小院；旧数据与旧运行不会被本增量接管。

在一个页面查看本机 Codex 身份与远端 Claude 对话，用完整身份键区分记录。远端消息与 Zellij 定位沿原有证据核验；三个只读 MCP 工具仍仅查询远端 Claude 索引。

当前首批固定来源为 `example@192.0.2.10`、`/home/example/.claude`、Claude Code `2.1.278`；本机 Zellij `0.45.1` 的 `workbench-v4 / Work-for-Linux`。元数据、存活进程和终端位置分别验证。此列表不是全服务器会话总数，也不判断任务是否完成。

## 前提与两种运行环境

本机 Python 3.10+、已有 SSH known_hosts/认证与 Terminal Workbench。观察不要求重启 Claude 或安装 Hooks；SSH 使用严格主机检查与非交互认证。

本机专属 `.venv` 只安装 `requirements.txt` 中的 MCP `1.26.0`。远端独立 reader 环境为 `/home/example/.local/share/session-panel-reader/.venv`，安装 `requirements-reader.txt` 中的 Claude Agent SDK `0.2.163` 与 MCP `1.26.0`。安装与真实兼容由协调者核验；不使用系统 pip，不改现有 CLI/PATH/Claude 设置。远端环境缺失时仍可观察身份，消息显示 `sdk_unavailable`。

```powershell
Set-Location 'C:/example/panel'
python -m venv .venv
& '.venv/Scripts/python.exe' -m pip install -r requirements.txt
# 新版 profile 启动命令；旧个人服务行为仅适用于原版本/已有实例。
& '.venv/Scripts/python.exe' -m session_panel.gateway --data-dir 'C:/example/SessionPanel/main' --port 14401
```

环境已存在时先核对归属和版本，不覆盖现有环境。原实例固定 8766 的说明为历史记录；新版端口由上文参数明确选择。

## 网页使用与状态

打开 `http://127.0.0.1:8766/`。远端 Claude 列表按 UUID 分行，即使两个会话 cwd 相同。点击明确的远端 ID 才读取最近 20 条可见消息；刷新观察只更新元数据。刷新最近消息与翻页才再次读取正文，隐藏页面暂停 10 秒一次的元数据轮询。

✅ 已实现：本机 Codex 只读身份索引，固定读取 `C:/example/private-69202ba3.sqlite` 的六个元数据字段，筛选未归档 cli/exec/vscode，每页 12 条；归档、子代理及其他来源数量另列。身份键使用 `codex:<UUID>`，与远端相同 UUID 仍是两个身份。选择 Codex 只显示身份详情、清空远端正文并禁用远端定位；失败或超过 30 秒的采样清空当前身份。内部格式变更时明确显示不可用。

会话证据区保留来源、身份、按需历史、终端位置和动作条件；使用列表按钮选择身份，支持键盘操作。3D/像素人物、巡游、粒子、场景控制与 Three 本地依赖已从源码移除；本轮验证状态见 [展示退役](docs/current/display-retirement-20261015/README.md)。历史图形交付报告保留原始结论，运行中的服务未在本轮重启。

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

网关由当前主 Codex 会话的 `CODEX_THREAD_ID` 启动且能够核验本机原生父子元数据时，首页默认进入“协作小院”。小院只列出这个主会话明确登记的子agent；点击“会话与终端观察”可回到原页面。相同目录或相同工作环境不会合并不同主会话。角色以原生文字按钮展示，任务状态来自真实收据。

当前闭环支持四种身份：主会话协调者登记子agent、分配固定产物、请求复核和验收；提交者用自己的会话身份交付；另一位已登记复核者用自己的身份接收复核并记录结论。每一步产生收据，产物与复核证据按版本和 SHA-256 核验。只有当前版本经过独立复核且由主会话验收后，摘要区才显示“完成交付”；展开工位可见完整身份、路径、哈希和历史收据。文件变更会使当前结果失效，重新交付会产生新版本。

主屏的伙伴名牌先显示登记角色、负责事项和已有收据阶段；点选名牌后，工位展示职责、能力、实际工作节点与时间。公开发言按需从当前主会话已登记子agent的本机记录中读取最近最多六条 assistant 公开发言；这是有限摘录，不是完整聊天，也不用于推断实时运行进度。任务摘要可展开或收起；原“会话与终端观察”保留终端操作条件。旧详情批次的历史证据见 [清晰主界面与角色详情](docs/current/subagent-garden-detail/README.md)，当前展示退役状态见本轮报告。

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
node tests/e2e/verify_workspace.cjs --fixture
node tests/e2e/verify_session_panel.cjs
node tests/e2e/verify_garden.cjs
```

真实 SSH 补绑、独立 SDK 合成链/空结果对照、选中真实 UUID 分页、Edge 桌面/360px、真实 focus 和 Codex 加载由协调者分别承接。fixture 自证不代表这些已验收；详细状态与所有失败见 `docs/current/unified-dev-control-panel/impl_report_r1.md` 及 `work/` 证据。

Ctrl+C 停网关即可停本批查询，原 Claude/SSH/Zellij 对话继续运行。撤销只移除本批新增源码/web/tests/requirements/根 README/example；真实 MCP 回退仅撤销本 server 表并由用户重载。删除本地 `.venv` 或远端 reader `.venv` 前确认绝对路径和本批归属。保留研究、方案和实施报告；不删除远端 `.claude`、项目或 Workbench。已经切换的 pane 由用户自行切回。
