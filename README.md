# 开发会话指挥室

开发记录仅保留在本机 ignored `docs/`，不随仓库提交。默认启动使用当前用户独立的空 profile，通过页面登记项目，不再要求复制个人 registry 文件。已有本机文件保留，可显式导入。

## 源码与测试目录

后端保留 `session_panel` 包，内部按 `http`、`sessions`、`workspace`、`execution`、`profiles`、`integrations` 六个功能包组织。前端使用原生 HTML/CSS 和普通脚本，页面资源放在 `web/sessions`、`web/progress`、`web/workspace`；普通启动直接消费已交付 JS，无需 Node、npm 或现场编译。公开页面和资源 URL 保持不变，例如 `/workspace.js` 对应物理文件 `web/workspace/workspace.js`。

本机观察与每日资讯分别以 `web/workspace/observation.ts`、`news.ts` 和邻近类型声明为手写源码，两个 JS 为同一固定 TypeScript 5.9.3 的生成物；其他模块继续手写 JS。修改对应 TS 后运行 `node scripts/build_observation.cjs build` 或 `node scripts/build_news.cjs build`，连同生成 JS 提交，再用对应 `check` 比较完整字节；不要手改生成物。Node24/项目内固定工具的显式准备、缺工具失败与全部验证命令见[观察前端 TypeScript 开发](CONTRIBUTING.md#观察前端-typescript-开发)。原 URL 和普通脚本相对顺序保留，新 `/news.js` 在页面主脚本前加载，不增加常驻 Node 服务。

测试按 `tests/backend`（Python）、`tests/frontend`（纯 Node 合同）、`tests/e2e`（浏览器/协议 harness）和 `tests/fixtures`（合成替身）归类。新增功能放入所属功能目录，测试按类型放置；详细职责与验证命令见 [参与开发](CONTRIBUTING.md)。原有 `python -m session_panel.gateway` 等外部 CLI 继续使用，内部代码直接导入功能包实现。

公开接入样例放在 `examples/integrations`，旧格式导入样例放在 `examples/legacy`；TypeScript、架构检查和远端 reader 的开发配置分别放在 `tools` 的对应目录。根目录保留项目入口说明、共同依赖及工具自动发现需要的配置。实际 profile、数据库和日志不属于源码树，不随目录整理搬迁。

多人开发同时检查文件归属与 Python 依赖方向；新增构建单元须连同规则和指南审查。HTTP 网关和页面主脚本负责装配，完整业务处理及展示放入所属模块。检查命令、允许依赖和合并要求见[参与开发](CONTRIBUTING.md#模块边界与新增代码)。

| 组成 | 使用边界 |
|---|---|
| Python 管理后端与前端资源 | 基础管理必需，负责项目、事项、进度与管理数据 |
| Rust 原生观察后台 | 完整本机观察的标准组件；缺失或故障时明确显示受限，基础管理仍可用 |
| Codex 元数据库、项目文档来源 | 用户按需登记；不配置也可人工管理，不自动读取私人来源 |
| SSH/远端 reader、终端、工作台、个人启动器 | 按需接入；终端依赖远端，工作台依赖终端，启动器可独立启用 |
| 公开资讯与官方额度 | 资讯按源容忍超时；额度仅显式查询，未配置账号不影响管理 |
| Node、TypeScript、Rust 编译器、浏览器测试工具 | 开发或验证工具，不属于普通用户的业务依赖 |

执行、审批、费用试跑及 Orca runtime 继续暂停；它们不是本次开放的可选功能。

公开接入样例放在 `examples/integrations`；`examples/legacy` 的 [项目登记样例](examples/legacy/progress-registry.example.json) 和 [人工进度样例](examples/legacy/progress-manual.example.json) 说明仍受支持的旧记录格式，供显式导入时参考。正常使用的数据保存在所选 profile；整理样例不会移动源码旁已有的私人文件。固定 TypeScript 配置与编译器清单位于 `tools/typescript`，远端正文 reader 的依赖单独位于 `tools/remote-reader/requirements.txt`；本机依赖仍使用根 `requirements.txt`。

## 多 Agent 工作台

统一入口 `/workspace` 提供项目登记、多个工作目录、人工进度与历史、任务和待办、可选文档进度来源、Codex 官方订阅额度、人工每日用量和固定来源 AI 资讯。项目不要求 Git 或特定工作流。人工进度、任务统计、文档观察分别展示；未填写百分比与真实 0 不混用，文档 PASS 或来源报告完成不会改写人工状态。

在自己的干净检出目录使用 Windows 与 Python 3.12 创建环境；已有 `.venv` 时核对归属与版本，不覆盖：

```powershell
python -m venv .venv
& '.venv/Scripts/python.exe' -m pip install -r requirements.txt
& '.venv/Scripts/python.exe' -m pip check
& '.venv/Scripts/python.exe' -m session_panel.profile init
& '.venv/Scripts/python.exe' -m session_panel.gateway
```

打开 `http://127.0.0.1:8766/workspace`。默认数据目录在 Windows 为 `%LOCALAPPDATA%/SessionPanel`（环境变量缺失时为当前用户 `AppData/Local/SessionPanel`）；目录内的 `profile.json` 保存来源设置，`workspace-state.sqlite3` 保存管理记录。路径优先级为 `--data-dir`、非空 `SESSION_PANEL_DATA_DIR`、当前用户默认目录；显式路径必须绝对。

保留已有服务时明确选择另一个端口，例如：

```powershell
& '.venv/Scripts/python.exe' -m session_panel.gateway --data-dir 'C:/example/SessionPanel/main' --port 14401
& './scripts/open-workspace.ps1' -Port 14401
```

随后访问 `http://127.0.0.1:14401/workspace` 或 `/progress`。网关只监听 `127.0.0.1`，端口范围为 1024–65535；占用即退出，不自动换端口、终止或接管旧服务。打开页面脚本只打开已经运行的服务。Ctrl+C停止Python网关；已构建的原生观察后台独立运行，显式停止方式见下节。

在“添加项目”填写名称与真实的主目录，可添加最多七个附加目录；搬迁时编辑路径，项目 ID 和绑定 ID 保持。归档保留任务、人工记录及待办，随时可恢复。新增任务只能选择活动项目；已有归档或未登记项目的任务可以保留原项目继续编辑，兼容记录区也可按原 ID 恢复登记。任务和用量列表提供分页，不以首批展示条数代表全集。

项目进度来源可选“未配置（人工管理）”、workflow-for-coding、普通文本或版本化 JSON。文本/JSON 使用选定目录内的相对路径；预设读取该目录的 `docs/current`，仅读有限直接子目录及 README/progress 文本。缺文件、空内容、出界或读取错误分别显示，不影响人工管理。JSON 示例：

```json
{"version":1,"state":"active","stage":"implementation","percent":0,"note":"等待测试","updated_at":"2026-10-15T08:00:00Z"}
```

除 version 外字段可选，但 state/stage/percent/note 至少一项有效。state 为 planned/active/blocked/completed；percent 为整数 0–100 或 NULL。单文件最多 256 KiB，预设每项目最多 64 个直接子目录、128 个候选文件；超预算明确报错，不把截断读取当作完整来源。

“来源与导入”可显式填写 Codex 元数据库的绝对路径并启用。只读六字段元数据，不读会话正文；禁用后不访问该库，也不回退开发者个人路径。新 profile 的远端 SSH、终端、工作台和启动器均为未配置；下节介绍可选接入的登记方式。高级接入配置不会启用执行 owner。额度缓存、用户点击后的官方只读额度查询和公开资讯仍保留；每日 Token/费用与 Grok 额度仍可人工记录。执行、审批、模型费用试跑及 Orca 接入沿用原有暂停边界。

## 每日 AI 资讯

首页仅展示浏览器本地今天、未标为未来的默认 AI 来源前两条；资讯页展示本次返回窗口内的今日、较早、未知和未来文章。默认来源仍是 Google AI、OpenAI News、Hugging Face Blog（简称 HF，含社区文章），每源最多20条，显示扫描/过滤/重复和截断限制；“未见今日内容”只针对本次返回窗口。Hacker News 可显式选择，旧 `/progress` 也消费同一来源缓存，标为非AI筛选，并将HN收录时间与文章出版时间区分。

卡片保留真实出版时间（缺失则未知）、来源更新、采样和上次成功时间；源有摘要才显示纯文本“来源摘要”或“来源摘要节选”。缓存15分钟，失败/过期保留旧日期并标旧；前端每分钟及恢复可见时重算，pending最多等待20秒，不表示后台被浏览器取消。资讯独立读取，不让待办或任务保存等待来源采集。公共源可能失败或变更格式，不能把缓存有效等同于来源有今日新闻。

本批没有自定义信息流、账号连接、模型主动联网检索、有引用的每日简报、持久每日记录、预算或调度；这些明确留待后批。

## Windows 本机观察与返回

Rust观察后台是完整本机观察的标准组件，按当前用户及profile隔离，Python网关绑定端口成功后才连接或启动固定的本机构建产物。缺少产物、身份或协议不匹配时，页面明确显示观察不可用，项目、任务和其他管理功能仍能使用。构建步骤和固定工具链准备见[参与开发](CONTRIBUTING.md#windows-原生观察开发)，HTTP不会临时编译或从PATH执行同名后台。

安装发现、已核验运行进程、历史会话、待处理及返回能力分别展示。Codex、Claude Code、Hermes、Pi按公开安装入口核验；不支持的安装形态、权限不足或缺少会话来源保持未知，不因发现工具读取默认账户库。CPU首样本未知，100%表示一个逻辑核；内存是进程工作集，不代表独占物理内存或Token/费用。进程发现约5秒、资源约1秒、安装缓存最多60秒，页面标明来源和时间。

“工具与环境登记”保留自动发现，支持确认内置Provider候选或手动填写本机非PATH入口，也可登记自定义EXE及终端GUI宿主。同一安装供多个项目使用；登记、安装核验、当前进程、会话来源与导航能力分别显示。自定义程序仅代表用户指定映像的进程/资源，不冒充官方Agent、会话或审批状态；通用解释器不按名字认领其所有脚本。登记和删除均不启动或结束工具。

“选择工具窗口”仅对当前已应用并核验的登记开放：显式列出合格可见窗口、有限显示标题，用户选择后只尝试切回该窗口，不建立Agent/pane关联。默认观察不采集标题；关闭/到期即丢弃选择，不读正文、不执行终端CLI或Lua、不新建窗口。Windows前台策略可能拒绝切换，页面显示实际结果。当前自动绑定和classic console返回保持受限、无动作许可；Windows Terminal/IDE/ConPTY不因此获得标签页定位，WezTerm可作为普通GUI宿主登记。

首次保存登记将profile配置升级为v3并先备份，旧四段集成仍使用原version2导入文件，Codex显式来源保持原语义。保存成功不等于后台已应用；待应用、失联或配置变化会撤销旧导航。备份含本机路径，恢复的新登记须重新确认定位，不恢复PID/窗口或动作许可。旧程序不读取v3，回退前保全新增意图并恢复升级前配置。

关闭浏览器/Python只断开观察连接，不结束外部Agent。原生CLI只管理自身观察后台，不停止任何外部工具；使用与网关相同的绝对profile目录：

```powershell
$observerData = Join-Path (Get-Location) 'work/example-profile'
& './work/native-observer/install/session-panel-observer.exe' status --data-dir $observerData
& './work/native-observer/install/session-panel-observer.exe' start --data-dir $observerData
& './work/native-observer/install/session-panel-observer.exe' stop --data-dir $observerData
```

该目录须已通过profile初始化；示例不会自动选择或接管其他服务。观察和导航不授予发送、审批、取消、启动Agent或读取正文的权限，真实工具版本/可见窗口覆盖需分别验收。

## 导入旧记录与恢复

管理库使用schema2，配置仍独立兼容v1/v2/v3；备份manifest1对应schema1，manifest2对应schema2。新空profile直接初始化schema2，已有schema1不会随启动自动升级：先正常停止自己使用该profile的实例，再显式执行离线迁移。命令不会终止其他程序；锁忙、未知表/版本、校验失败或超预算会拒绝并保留原库及已生成备份，不截断历史值。

```powershell
& '.venv/Scripts/python.exe' -m session_panel.profile status --data-dir 'C:/example/SessionPanel/main'
& '.venv/Scripts/python.exe' -m session_panel.profile migrate-tasks --data-dir 'C:/example/SessionPanel/main'
```

迁移将旧tasks/todos收为一个可写模型，保留来源及原ID（包括空待办ID）、重复标题、原顺序、时间和NULL；旧接口通过适配读写同一模型。数据库中的tasks/todos是只读兼容视图，不能再用旧程序或直接SQL写入。新列表的顺序单独保存，不伪造两种来源的历史先后。

在空 profile 的“来源与导入”填写旧数据目录的绝对路径，例如 `C:/example/previous-panel`。固定读取 `progress-registry.json`，以及可选的 `progress-manual.json`、`progress-todos.json`、`workspace-state.sqlite3`。只导入全部合法项目、人工进度、待办、工作台任务和人工用量；保留原 ID、顺序、关联与已有时间。未登记项目的记录进入兼容区；没有原时间的导入快照显示“来源未提供时间”。

导入不改源目录、不读或复制 execution/审批/事件/私人正文，不导入个人来源设置。非法、重复、schema 不兼容或超预算的批次整批拒绝并给出字段定位；不会只取旧页面前 500/100 条。目的 profile 必须业务为空且未导入过，来源与目的目录不能重合或互相包含。已有记录应另选空 `--data-dir`，不能覆盖或合并导入。

提交前会在目的 `backups/<UTC>-<random>/` 生成管理库、配置快照和 manifest。成功页面显示完整 counts、备份路径及排除范围；备份失败不提交导入，失败的 `.pending-*` 目录可能留作诊断。恢复在离线命令中写到另一个不存在或真正空的目录，拒绝覆盖已有 profile：

```powershell
& '.venv/Scripts/python.exe' -m session_panel.profile restore --backup 'C:/example/SessionPanel/main/backups/<备份目录>' --data-dir 'C:/example/SessionPanel/restored'
# Only when restore reports schema1 / migration_required:
& '.venv/Scripts/python.exe' -m session_panel.profile migrate-tasks --data-dir 'C:/example/SessionPanel/restored'
& '.venv/Scripts/python.exe' -m session_panel.gateway --data-dir 'C:/example/SessionPanel/restored' --port 14401
```

恢复会校验 manifest、文件大小/哈希、schema 与 SQLite 完整性。停止自己的旧实例后另行启动恢复目录；不在线切换，不删除原 profile 或导入后的新记录。真实个人数据是否符合导入合同需实际校验，隔离 fixture 验证不能替代个人迁移结果。

恢复旧备份会先保留其schema1结构，再由上述命令显式迁移。需要退回旧程序时，先停新版实例并运行 `python -m session_panel.profile preserve-tasks --data-dir <绝对profile目录>`，验证包含升级后全部新增/变更的schema2私密备份，再把升级前旧备份恢复到另一个空目录。原schema2目录和保全件必须保留；保全不是把新语义自动反写旧库，单独回退代码不代表数据已无损降级。

新记录保存在所选 profile，源码旁旧 JSON/SQLite 与既有服务保留原位。任务负责人表示人工分配，不向 Agent 发送命令。资讯含来源与采样时间，不是推送流。

历史桌面与移动尺寸验收证据仅保留在本地开发记录。手机远程访问、自动每日成本和原生安装包仍是后续能力。

## 配置可选接入

基础项目管理无需 SSH、Zellij、WezTerm 或 reader。已有用户也通过自己的 profile 登记这些资源；源码不提供个人服务器、工具路径、完整性摘要或启动器默认值。设置页显示每段的“未配置 / 已禁用 / 已配置”和重启提示。“已配置”仅表示通过配置校验，实际连接还会核验来源、版本和身份。

高级文件采用 version 2，包含固定四段 remote、terminal、workbench、launchers。随仓库提供的 [integration-settings.example.json](examples/integrations/integration-settings.example.json) 四段均为 null，可直接校验。首次复制到 ignored `work/` 后编辑自己的值；已有本机配置文件时直接编辑，保留其现有内容：

```powershell
New-Item -ItemType Directory -Force work | Out-Null
Copy-Item examples/integrations/integration-settings.example.json work/integration-settings.json
$settingsFile = (Resolve-Path work/integration-settings.json).Path
& '.venv/Scripts/python.exe' -m session_panel.profile validate-settings --file $settingsFile
& '.venv/Scripts/python.exe' -m session_panel.profile import-settings --file $settingsFile
& '.venv/Scripts/python.exe' -m session_panel.profile status
```

使用独立数据目录时，为 init、import-settings、status 和 gateway 都传同一个绝对 `--data-dir`。validate-settings 只校验该文件，不写 profile、不探测工具或网络。import-settings 备份后整体替换四段，保留 Codex 设置和管理记录；文件中的 null 会清除该段，enabled=false 保留完整配置但禁用。编辑时保留仍需使用的其他段，不能把单段文件当增量补丁。配置写入竞争返回 config_busy，可在另一个写入结束后重试。

导入成功后停止自己的网关，再用同一 data-dir/端口启动。运行中的接入保留启动快照，网页显示重启提示；保存 Codex 设置仍即时生效且不会丢失高级配置。旧 version 1 profile 可以继续读取，更新时保留备份并写入 version 2。网页只提供 Codex 设置和高级接入状态，高级配置通过上述本机命令导入。

下面是四段启用的完整结构示例。**所有地址、路径和摘要均为合成值，必须换成自己的真实资源；示例通过语法校验不代表资源可用。** 也可从全 null 文件开始，只填写所需段；terminal 启用要求 remote 已启用，workbench 启用要求 terminal 已启用，launchers 可独立启用。

```json
{
  "version": 2,
  "integrations": {
    "remote": {
      "enabled": true,
      "source": {
        "id": "example@192.0.2.10:/home/example/.claude",
        "host": "192.0.2.10",
        "user": "example",
        "config_dir": "/home/example/.claude",
        "claude_version": "2.1.278"
      },
      "home": "/home/example",
      "ssh": "ssh.exe",
      "reader_python": "/home/example/session-panel-reader/.venv/bin/python",
      "reader_mcp_version": "1.30.0",
      "slot_files": {"main": "/home/example/slots/main.cwd"}
    },
    "terminal": {
      "enabled": true,
      "zellij": "C:/example/tools/zellij.exe",
      "session": "example-session",
      "tab": "Example tab",
      "version": "0.45.1",
      "pane_script": "~/.local/share/example/pane.sh",
      "ssh_executables": ["ssh.exe"]
    },
    "workbench": {
      "enabled": true,
      "wezterm_gui": "C:/example/tools/wezterm-gui.exe",
      "wezterm_lua": "C:/example/config/wezterm.lua",
      "zellij_config": "C:/example/config/zellij.kdl",
      "workdir": "C:/example/projects",
      "unmanaged_mux": "C:/example/tools/wezterm-mux-server.exe",
      "pins": {
        "zellij": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "wezterm_gui": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "wezterm_lua": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "zellij_config": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      }
    },
    "launchers": {
      "enabled": true,
      "groups": [{"id": "tools", "name": "常用工具", "blurb": "自己登记的本机入口"}],
      "items": [
        {"id": "terminal", "group": "tools", "name": "打开终端", "blurb": "自己的启动脚本", "kind": "cmd", "target": "C:/example/工具 脚本/启动.cmd", "probe": ""},
        {"id": "menu", "group": "tools", "name": "打开菜单", "blurb": "自己的 Python 入口", "kind": "py", "target": "C:/example/tools/menu.py", "probe": ""},
        {"id": "task", "group": "tools", "name": "运行已登记任务", "blurb": "已有系统任务", "kind": "task", "target": "ExampleTask", "probe": "example.exe"},
        {"id": "page", "group": "tools", "name": "打开本机页面", "blurb": "已有本机服务", "kind": "url", "target": "http://127.0.0.1:19999/", "probe": "http://127.0.0.1:19999/"}
      ]
    }
  }
}
```

remote 的 source 必须与现有远端用户、home、Claude 配置目录和版本一致；SSH 认证和 known_hosts 由系统管理，继续使用严格主机检查与非交互认证，不在配置中存放密钥、密码或 token。source.id 是稳定的来源标识，迁移时保留原值。当前支持 Claude Code 2.1.278、Zellij 0.45.1，其他版本会被拒绝，不能任意填写“最新版”。

terminal 只匹配登记的 `<ssh_executable> -t <host> bash <pane_script> <slot>`，slot 取自 remote.slot_files；带空格的可执行路径必须是标准双引号形式。面板不会安装这些工具、创建远端 slot 文件或启动 Zellij 后台。先准备自己的现有工作台，再登记参数。

workbench 的 pins 必须为 zellij、wezterm_gui、wezterm_lua、zellij_config 四个文件各自的 SHA-256。先核验文件来源和内容，再用 `Get-FileHash -Algorithm SHA256 -LiteralPath <文件绝对路径>` 获取摘要。文件变更后旧摘要会阻止动作，面板不会自动重算并信任新文件；确认变化后才更新本机配置。

launchers 仅由已登记 id 触发，页面不能传目标或额外参数。cmd 只支持本机盘符绝对路径的 `.cmd` / `.bat`，中文与空格允许；拒绝 UNC/设备路径、相对路径、`.`/`..`、路径组件尾点/尾空格、附加参数、控制字符、双引号及 `& | < > ^ % ! ( ) ? *`，除盘符外也不能有冒号。若原入口不满足限制，可自行准备安全命名的脚本副本或入口再登记，面板不会改名私人文件。Windows 下打开独立控制台，脚本正常 `exit /b` 后保留提示符；脚本主动 `exit` 则退出。py 只支持绝对 `.py` / `.pyw`，使用当前 Python 独立参数调用；task 运行已有系统任务，url 使用系统浏览器打开无用户密码的 http/https 地址。cmd/task 在非 Windows 平台明确不可用。

## 本机与远端 reader 环境

本机 `.venv` 安装 requirements.txt 的 MCP 1.30.0。需要远端消息时，另在自己的远端专用虚拟环境安装 tools/remote-reader/requirements.txt（Claude Agent SDK 0.2.163、MCP 1.30.0），再填写 reader_python 和 reader_mcp_version。不要覆盖现有虚拟环境、系统 pip 或 Claude 配置；安装后单独验证 reader 身份与能力。

远端 MCP 1.26.0 仅作为明确配置的旧环境迁移兼容值，设置摘要会提示升级；本机依赖升级不会升级远端，也不证明旧远端的依赖风险已处理。reader 缺失时消息显示 sdk_unavailable；已支持的版本不等于你的远端已验证。管理记录导入和高级参数导入是两个独立操作，旧 registry 导入不会自动迁移 SSH、工具路径或启动器。

## 网页使用与状态

打开 `http://127.0.0.1:8766/`。远端 Claude 列表按 UUID 分行，即使两个会话 cwd 相同。点击明确的远端 ID 才读取最近 20 条可见消息；刷新观察只更新元数据。刷新最近消息与翻页才再次读取正文，隐藏页面暂停 10 秒一次的元数据轮询。

本机 Codex 只读身份索引使用“来源与导入”中显式登记的数据库路径，读取六个元数据字段，筛选未归档 cli/exec/vscode，每页 12 条；归档、子代理及其他来源数量另列。身份键使用 `codex:<UUID>`，与远端相同 UUID 仍是两个身份。选择 Codex 只显示身份详情、清空远端正文并禁用远端定位；失败或超过 30 秒的采样清空当前身份。内部格式变更时明确显示不可用。

会话证据区保留来源、身份、按需历史、终端位置和动作条件；使用列表按钮选择身份，支持键盘操作。3D/像素人物、巡游、粒子、场景控制与 Three 本地依赖已从源码移除。历史交付记录保留在本机 docs/。

Codex 正文、实时活动监控和全服务器会话覆盖仍未提供。远端 reader、终端关联与定位是否可用，必须用自己的配置分别验证；合成测试不代表私人环境已接入。

身份 `verified`、进程 `observed_running`、位置 `confirmed` 是独立证据；活动和项目完成始终 `unknown`。断连或来源失败立即显示未知，并保留“上次证据”；超过 30 秒的证据不作当前状态。定位需要 5 秒内的唯一实例和当前 pane，点击后重新核验，只切换已有 Zellij pane，不输入字符或激活桌面窗口。命令发送后还须唯一客户端焦点和 pane 清单确认，否则显示 `focus_sent_unverified`。

WezTerm 状态区单独观察登记的 Zellij session 后台、GUI 进程、交互客户端与证据年龄；来源绿点仅表示远端采样。只有后台存活、未关联且证据足够时，才能显式“连接现有工作台”。已有连接不会重复创建；已退出或不存在的后台不会复活或新建。外部独立 mux 仅计数，不采用或关闭。GUI 可见性和 watcher 覆盖未核验，成功只称“唯一关联观察”，不是 client-PID 握手或原子实例绑定。

选择 UUID 后，定位还要求 5 秒内的唯一工作台关联，动作前后重新检查原实例、客户端和 pane。显示“已确认 Zellij 选中 pane”后，请切到 WezTerm 查看。刷新终端状态或连接结果不会重新读取消息。若出现 `cleanup_unverified`，页面禁用连接与定位，请核查本次新进程的回收状态；不提供自动重启或解除入口。

消息使用官方 reader 的六字段，单条消息时间省略；“本页读取时间”只表示读取时点。`reader_scope_unverified` 表示目录与 reader 查找范围不能证明一致；`reader_empty_unverified` 表示 reader 返回空结果，真正空历史也未验证。它们不会显示为“暂无对话/读取成功”。文件变化返回 `messages_changed` 或 `revision_mismatch`，请刷新最近消息。文件上限 16 MiB、读取 20 秒、响应 512 KiB、每页 1–50 条；超预算不会静默截断。

正文只留在请求/页面内存，不写磁盘、浏览器存储或研究日志；不默认加载消息图片/附件，不发给其它外部模型。页面错误保留当前选择和上次消息结果。

## Codex MCP 接入

先保持已配置 remote 的网关在 127.0.0.1:8766 运行；当前 stdio 桥固定访问该端口。使用其他端口可正常使用网页，但不能把该实例视为当前 MCP 桥的目标。将示例中的 command/cwd 换成自己的检出路径。`.codex/config.toml.example` 是可审阅的示例，协调者只合并该 server 表到受信任项目的 `.codex/config.toml`，不覆盖其它服务器或写全局配置。

```toml
[mcp_servers.remote_claude_sessions]
command = "C:/example/session-panel/.venv/Scripts/python.exe"
args = ["-m", "session_panel.mcp_stdio"]
cwd = "C:/example/session-panel"
enabled_tools = ["list_sessions", "get_session_status", "get_session_messages"]
```

工具仅有 `list_sessions()`、`get_session_status(session_id)`、`get_session_messages(session_id, offset=None, limit=20, revision=None)`；没有 focus、发送、审批、resume 或终止工具。stdio stdout 只发送 MCP 协议，网关未运行会明确返回 `gateway_unavailable`，不会自动再建索引。

保存配置不代表当前聊天已加载。依照 [Codex 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=app)由用户控制客户端正常重载；不静默重启当前 Codex。`codex mcp list` 只证明配置，真正接入还需加载后发现恰好三个工具，并按两个真实 ID 调用，与网页结果核对。内部 `8766` HTTP 地址不是 MCP URL。

## 验证与撤销

基础开发验证使用 Windows、Python 3.12、Node.js 24；无需作者资源。先按[参与开发](CONTRIBUTING.md#观察前端-typescript-开发)准备共享固定编译器，再运行两模块 check 和八份 Node 合同。浏览器 harness 的 Playwright/Edge 安装与 fixture 执行见 [参与开发](CONTRIBUTING.md)。以下浏览器命令由验收负责人执行，结果与单元测试分别记录：

```powershell
& '.venv/Scripts/python.exe' -m unittest discover -s tests -v
node scripts/build_observation.cjs check
node scripts/build_news.cjs typecheck
node scripts/build_news.cjs check
node --test tests/frontend/test_execution_ui.cjs tests/frontend/test_progress_ui.cjs tests/frontend/test_workspace_history_ui.cjs tests/frontend/test_observation_ui.cjs tests/frontend/test_task_center_ui.cjs tests/frontend/test_observation_build.cjs tests/frontend/test_news_ui.cjs tests/frontend/test_news_build.cjs
node tests/e2e/verify_workspace.cjs --fixture
node tests/e2e/verify_session_panel.cjs --fixture
```

真实 SSH 补绑、独立 SDK 合成链/空结果对照、选中真实 UUID 分页、Edge 桌面/360px、真实 focus 和 Codex 加载由协调者分别承接。fixture 自证不代表这些已验收；详细状态与所有失败保留在本地开发记录及 ignored `work/`，不提交仓库。

Ctrl+C 停网关即可停本批查询，原 Claude/SSH/Zellij 对话继续运行。代码回退需与对应 profile 版本配套，先保留备份，再在另一个空数据目录恢复并验证；真实 MCP 回退仅撤销本 server 表并由用户重载。删除本地 `.venv` 或远端 reader `.venv` 前确认绝对路径和本批归属。保留研究、方案和实施报告；不删除远端 `.claude`、项目或 Workbench。已经切换的 pane 由用户自行切回。

## 公开内容与本机数据

实际 profile、导入文件、备份、数据库、凭据、日志、图片视频及开发记录只保留本机；配置备份同样可能含私人服务器和路径，不提交或上传公开 artifact。`work/`、`docs/`、`videos/` 为本机目录，忽略规则不会自动取消已跟踪文件。当前版本的清理不等于公开 Git 历史已清理；历史改写、强推与删除分支需要独立授权。检查器结果只覆盖报告列出的范围。

新成员可按 [参与开发](CONTRIBUTING.md) 从干净检出安装并执行 Python/Node 测试，再用独立数据目录完成初始化、项目登记和设置验证。仓库规则是开发要求；远端 CI、分支保护是否生效必须检查实际运行与 effective ruleset，不能仅凭规则文件宣称已启用。
