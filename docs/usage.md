# 使用指南

[返回项目首页](../README.md) · [使用指南](usage.md) · [可选接入](integrations.md) · [Windows 安装与维护](windows.md)

本文命令均从仓库根目录执行；安装版的专用命令另行注明。

## 多 Agent 工作台

统一入口 `/workspace` 提供项目与事项管理、本机 Codex 项目和会话读取、官方订阅额度及固定来源 AI 新闻。左侧选择项目，右侧保留“全部事项、待处理、会话”；待处理筛选人工标为阻塞或待验收的事项。用量页展示来源读取结果。手工项目不要求 Git 或特定工作流，原有进度、历史和文档来源数据继续保留。

以下为源码开发入口，使用 Windows 与 Python 3.12 及以上版本。先安装 editable 包，再使用原模块 CLI；仅安装 requirements 不会安装 `src` 下的程序。已有 `.venv` 时核对归属与版本，不覆盖：

Python 包已统一为 `control_everything`，分发名为 `control-everything`。更新已有检出后重新运行 `python -m pip install -e .`，将自己的启动命令改为 `python -m control_everything.gateway`；旧包名命令不再作为源码入口。默认数据目录、`SESSION_PANEL_DATA_DIR` 和已有安装身份保留，源码改名不会迁移或清空数据；正在运行的旧源码实例需自行正常停止后用新命令启动。

```powershell
git clone https://github.com/zxl123456123/control-everything.git
cd control-everything
python -m venv .venv
& '.venv/Scripts/python.exe' -m pip install -e .
& '.venv/Scripts/python.exe' -m pip check
& '.venv/Scripts/python.exe' -m control_everything.profile init
& '.venv/Scripts/python.exe' -m control_everything.gateway
```

打开 `http://127.0.0.1:8766/workspace`。默认数据目录在 Windows 为 `%LOCALAPPDATA%/SessionPanel`（环境变量缺失时为当前用户 `AppData/Local/SessionPanel`）；目录内的 `profile.json` 保存来源设置，`workspace-state.sqlite3` 保存管理记录。路径优先级为 `--data-dir`、非空 `SESSION_PANEL_DATA_DIR`、当前用户默认目录；显式路径必须绝对。

标准 Python wheel 包含后端和前端资源，可以离开检出目录使用。它不包含 Rust 原生程序，缺少该程序时观察显示 `native_missing`，基础管理仍可启动；Windows 标准安装另外组合固定私有运行时和 release 原生程序，并严格核验产品闭包。源码模式保留旧根数据位置；安装包的兼容旧记录默认目录为用户默认数据目录下的 `legacy`，不会写入 site-packages，也不会自动搬迁旧记录。

保留已有服务时明确选择另一个端口，例如：

```powershell
& '.venv/Scripts/python.exe' -m control_everything.gateway --data-dir 'C:/example/SessionPanel/main' --port 14401
& './tools/scripts/open-workspace.ps1' -Port 14401
```

随后访问 `http://127.0.0.1:14401/workspace` 或 `/progress`。网关只监听 `127.0.0.1`，端口范围为 1024–65535；占用即退出，不自动换端口、终止或接管旧服务。打开页面脚本只打开已经运行的服务。Ctrl+C停止Python网关；已构建的原生观察后台独立运行，显式停止方式见下节。

“添加项目”提供“从已有项目选择”和“新建项目”两条入口。从已有项目选择时，选择 Codex、Claude Code 或 OpenCode，先确认项目来源，再点击、勾选或从本页候选拖入想管理的项目。发现候选不会自动加入，以后新发现的项目也需要选择。没有可识别项目、尚未确认来源或读取失败时，都可直接“新建项目”，填写名称与真实的主目录，无需读取工具来源；可添加最多七个附加目录。搬迁时编辑路径，项目 ID 和绑定 ID 保持。归档保留任务、人工记录及待办，随时可恢复。新增任务只能选择活动项目；已有归档或未登记项目的任务可以保留原项目继续编辑，兼容记录区也可按原 ID 恢复登记。任务列表提供分页，不以首批展示条数代表全集。

项目进度来源可选“未配置（人工管理）”、workflow-for-coding、普通文本或版本化 JSON。文本/JSON 使用选定目录内的相对路径；预设读取该目录的 `docs/current`，仅读有限直接子目录及 README/progress 文本。缺文件、空内容、出界或读取错误分别显示，不影响人工管理。JSON 示例：

```json
{"version":1,"state":"active","stage":"implementation","percent":0,"note":"等待测试","updated_at":"2026-10-15T08:00:00Z"}
```

除 version 外字段可选，但 state/stage/percent/note 至少一项有效。state 为 planned/active/blocked/completed；percent 为整数 0–100 或 NULL。单文件最多 256 KiB，预设每项目最多 64 个直接子目录、128 个候选文件；超预算明确报错，不把截断读取当作完整来源。

“设置与导入”可填写本机 Codex 数据库的绝对路径并启用。工作台只读所选库及相邻 `.codex-global-state.json` 的项目、目录、会话标题、时间和归属元数据，不读正文或登录凭据。桌面项目作为候选展示，显式加入后才出现在工作台，项目身份按来源和显式项目 ID 区分；即使两个项目共用目录也不会自动合并。迁移中的旧新项目 ID 与会话归属先合并，再按项目分页；未知归属与显式无项目分别展示，读取不完整或失败会明确标识。

Codex 项目由来源管理名称和目录。显式加入时，工作台先把项目、目录和来源保存到自己的管理库；后续事项继续在同库事务内校验；刷新页面不会自动创建事项。可从会话页显式打开事项填写，也可在原事项编辑器中更换所属项目，原事项 ID 保留。来源暂不可用时，已保存的项目与事项仍保留，原事项可继续修改状态，新关联需等来源恢复。禁用后不再访问来源，也不回退其他个人路径。

Claude Code 与 OpenCode 列出的是“识别到的项目目录”，不代表导入完整桌面配置。[Claude Code 桌面端与 CLI 共用配置文件](https://code.claude.com/docs/en/desktop#shared-configuration)；工作台只支持明确确认的全局 JSON 中 `projects` 对象的绝对目录键，成员内容与聊天正文不保存。[OpenCode 桌面端运行本机服务并保存项目数据](https://docs.opencode.ai/docs/troubleshooting/)；工作台支持已有 v1.2.27 证据的 `project` 表结构，只取项目目录，未知格式（包括未核实的 V2）可直接新建项目。Codex 候选保留来源中的项目名称和目录；来源位置参考当前 `CODEX_HOME` 和用户目录，OpenCode 参考 `XDG_DATA_HOME`。位置建议不代表读取许可，多个文件需明确选择。`CLAUDE_CONFIG_DIR` 只提示目录，请填写实际全局文件。禁用立即停止该来源读取。

“移出工作台”保留事项和历史，可在添加面板的“之前管理过”重新加入；移出与业务归档独立。来源丢失仍保留已存项目。Claude/OpenCode 导入后可改名、改目录或配置进度来源；Codex 名称与目录由原来源管理。多个来源的同目录项目保持独立；变更导入目录仍检查活动项目冲突。

程序登记位于“工具与接入 → 配置工具”；添加项目面板只处理项目和项目来源。Claude/OpenCode 原生 EXE 与其他工具的自定义 EXE 必须确认本机路径，校验的是文件身份，不保证发布者签名；不接受 node/python/cmd 等通用宿主。登记不会运行、安装或结束工具。普通工具页只展示存在/登记状态，进程资源在折叠诊断中查看。页面自动刷新，错误时提供局部重试；外部资源管理器目录拖入请改用新建项目的手工路径入口。

v4 备份恢复保留项目、路径和移出意图，但停用三个工具的来源许可，需在新目录重新确认读取；原生登记沿用重新绑定规则。恢复不恢复进程或窗口许可，回退前应保全当前备份并在另一目录恢复升级前版本，不要手工删字段降版。

新 profile 的远端 SSH、终端、工作台和启动器均为未配置；登记方式见[可选接入](integrations.md)。高级接入配置不会启用执行 owner。官方额度仅在点击查询后读取，公开资讯独立加载。当前不开放 Agent 执行、审批或模型费用试跑。

## 每日 AI 资讯

首页仅展示浏览器本地今天、未标为未来的默认 AI 来源前两条；资讯页展示本次返回窗口内的今日、较早、未知和未来文章。默认来源仍是 Google AI、OpenAI News、Hugging Face Blog（简称 HF，含社区文章），每源最多20条，显示扫描/过滤/重复和截断限制；“未见今日内容”只针对本次返回窗口。Hacker News 可显式选择，旧 `/progress` 也消费同一来源缓存，标为非AI筛选，并将HN收录时间与文章出版时间区分。

卡片保留真实出版时间（缺失则未知）、来源更新、采样和上次成功时间；源有摘要才显示纯文本“来源摘要”或“来源摘要节选”。缓存15分钟，失败/过期保留旧日期并标旧；前端每分钟及恢复可见时重算，pending最多等待20秒，不表示后台被浏览器取消。资讯独立读取，不让待办或任务保存等待来源采集。公共源可能失败或变更格式，不能把缓存有效等同于来源有今日新闻。

本批没有自定义信息流、账号连接、模型主动联网检索、有引用的每日简报、持久每日记录、预算或调度；这些明确留待后批。

## Windows 本机观察与返回

Rust观察后台是完整本机观察的标准组件，按当前用户及profile隔离，Python网关绑定端口成功后才连接或启动固定产物。标准产品只使用所属版本下已核验的 `native/observer`，源码开发使用固定的本机构建目录；不会临时编译或从PATH执行同名后台。源码/wheel 缺少产物时明确显示观察不可用；标准产品的基础运行文件损坏会阻止该版本启动，原生程序或其摘要文件缺失、损坏时基础管理仍可启动、观察明确受限。保留资料并使用可信安装器维护，安装与切换版本仍须完整核验所有交付文件。源码构建步骤和固定工具链准备见[参与开发](../.github/CONTRIBUTING.md#windows-原生观察开发)。

安装发现、已核验运行进程、历史会话、待处理及返回能力分别展示。Codex、Claude Code、OpenCode 提供默认程序入口，Hermes、Pi 保留原接入；不支持的安装形态、权限不足或缺少会话来源保持未知，不因发现工具读取默认账户库。CPU首样本未知，100%表示一个逻辑核；内存是进程工作集，不代表独占物理内存或Token/费用。进程发现约5秒、资源约1秒、安装缓存最多60秒，页面标明来源和时间。

“工具与环境登记”保留自动发现，支持确认内置Provider候选或手动填写本机非PATH入口，也可登记自定义EXE及终端GUI宿主。同一安装供多个项目使用；登记、安装核验、当前进程、会话来源与导航能力分别显示。自定义程序仅代表用户指定映像的进程/资源，不冒充官方Agent、会话或审批状态；通用解释器不按名字认领其所有脚本。登记和删除均不启动或结束工具。

“选择工具窗口”仅对当前已应用并核验的登记开放：显式列出合格可见窗口、有限显示标题，用户选择后只尝试切回该窗口，不建立Agent/pane关联。默认观察不采集标题；关闭/到期即丢弃选择，不读正文、不执行终端CLI或Lua、不新建窗口。Windows前台策略可能拒绝切换，页面显示实际结果。当前自动绑定和classic console返回保持受限、无动作许可；Windows Terminal/IDE/ConPTY不因此获得标签页定位，WezTerm可作为普通GUI宿主登记。

普通旧登记仍兼容 v3；首次保存项目来源、移出意图或新程序类型时先备份并升级 profile v4，旧四段集成仍使用原version2导入文件，Codex显式来源保持原语义。保存成功不等于后台已应用；待应用、失联或配置变化会撤销旧导航。备份含本机路径，恢复的新登记须重新确认定位，不恢复PID/窗口或动作许可。旧程序不读取新配置版本，回退前保全新增意图并恢复升级前配置。

关闭浏览器/Python只断开观察连接，不结束外部Agent。原生CLI只管理自身观察后台，不停止任何外部工具；使用与网关相同的绝对profile目录：

```powershell
$observerData = Join-Path (Get-Location) 'work/example-profile'
& './work/native-observer/install/session-panel-observer.exe' status --data-dir $observerData
& './work/native-observer/install/session-panel-observer.exe' start --data-dir $observerData
& './work/native-observer/install/session-panel-observer.exe' stop --data-dir $observerData
```

以上为源码开发产物路径；安装用户需使用该 profile 原来所属的 `versions/<版本>-<闭包前16位>/native/observer/session-panel-observer.exe`，不要从 PATH 或另一版本选择同名程序。目录须已通过profile初始化；示例不会自动选择或接管其他服务。观察和导航不授予发送、审批、取消、启动Agent或读取正文的权限，真实工具版本/可见窗口覆盖需分别验收。

## 导入旧记录与恢复

管理库使用schema2，配置仍独立兼容v1/v2/v3/v4；备份manifest1对应schema1，manifest2对应schema2。新空profile直接初始化schema2，已有schema1不会随启动自动升级：先正常停止自己使用该profile的实例，再显式执行离线迁移。命令不会终止其他程序；锁忙、未知表/版本、校验失败或超预算会拒绝并保留原库及已生成备份，不截断历史值。

```powershell
& '.venv/Scripts/python.exe' -m control_everything.profile status --data-dir 'C:/example/SessionPanel/main'
& '.venv/Scripts/python.exe' -m control_everything.profile migrate-tasks --data-dir 'C:/example/SessionPanel/main'
```

迁移将旧tasks/todos收为一个可写模型，保留来源及原ID（包括空待办ID）、重复标题、原顺序、时间和NULL；旧接口通过适配读写同一模型。数据库中的tasks/todos是只读兼容视图，不能再用旧程序或直接SQL写入。新列表的顺序单独保存，不伪造两种来源的历史先后。

在空 profile 的“来源与导入”填写旧数据目录的绝对路径，例如 `C:/example/previous-panel`。固定读取 `progress-registry.json`，以及可选的 `progress-manual.json`、`progress-todos.json`、`workspace-state.sqlite3`。只导入全部合法项目、人工进度、待办、工作台任务和人工用量；保留原 ID、顺序、关联与已有时间。未登记项目的记录进入兼容区；没有原时间的导入快照显示“来源未提供时间”。

导入不改源目录、不读或复制 execution/审批/事件/私人正文，不导入个人来源设置。非法、重复、schema 不兼容或超预算的批次整批拒绝并给出字段定位；不会只取旧页面前 500/100 条。目的 profile 必须业务为空且未导入过，来源与目的目录不能重合或互相包含。已有记录应另选空 `--data-dir`，不能覆盖或合并导入。

提交前会在目的 `backups/<UTC>-<random>/` 生成管理库、配置快照和 manifest。成功页面显示完整 counts、备份路径及排除范围；备份失败不提交导入，失败的 `.pending-*` 目录可能留作诊断。恢复在离线命令中写到另一个不存在或真正空的目录，拒绝覆盖已有 profile：

```powershell
& '.venv/Scripts/python.exe' -m control_everything.profile restore --backup 'C:/example/SessionPanel/main/backups/<备份目录>' --data-dir 'C:/example/SessionPanel/restored'
# Only when restore reports schema1 / migration_required:
& '.venv/Scripts/python.exe' -m control_everything.profile migrate-tasks --data-dir 'C:/example/SessionPanel/restored'
& '.venv/Scripts/python.exe' -m control_everything.gateway --data-dir 'C:/example/SessionPanel/restored' --port 14401
```

恢复会校验 manifest、文件大小/哈希、schema 与 SQLite 完整性。停止自己的旧实例后另行启动恢复目录；不在线切换，不删除原 profile 或导入后的新记录。真实个人数据是否符合导入合同需实际校验，隔离 fixture 验证不能替代个人迁移结果。

恢复旧备份会先保留其schema1结构，再由上述命令显式迁移。需要退回旧程序时，先停新版实例并运行 `python -m control_everything.profile preserve-tasks --data-dir <绝对profile目录>`，验证包含升级后全部新增/变更的schema2私密备份，再把升级前旧备份恢复到另一个空目录。原schema2目录和保全件必须保留；保全不是把新语义自动反写旧库，单独回退代码不代表数据已无损降级。

新记录保存在所选 profile，源码旁旧 JSON/SQLite 与既有服务保留原位。任务负责人表示人工分配，不向 Agent 发送命令。资讯含来源与采样时间，不是推送流。

历史桌面与移动尺寸验收证据仅保留在本地开发记录。手机远程访问、自动每日成本仍是后续能力。

## 公开内容与本机数据

实际 profile、导入文件、备份、数据库、凭据和日志只保留本机；配置备份可能包含私人服务器和路径，不提交到公开仓库或上传为公开附件。

新成员可按 [参与开发](../.github/CONTRIBUTING.md) 从干净检出安装并执行 Python/Node 测试，再用独立数据目录完成初始化、项目登记和设置验证。仓库规则是开发要求；远端 CI、分支保护是否生效必须检查实际运行与 effective ruleset，不能仅凭规则文件宣称已启用。
