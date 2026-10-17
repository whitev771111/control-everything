# 参与开发

仓库协作约定见 [AGENTS.md](AGENTS.md)。本指南约束共同交付结果，不要求成员使用同一助手、模型或个人技能。

## 功能目录与新增代码归属

管理后端沿用 Python，Windows 进程观察由 Rust 组件负责；前端沿用 HTML/CSS/JS，不增加前端构建或包安装层。后端按实际职责组织，新增代码优先放入下表对应功能包；不为每个功能预建空的通用分层。

| 后端目录 | 职责 |
|---|---|
| `session_panel/http` | HTTP 路由、静态白名单与服务装配 |
| `session_panel/sessions` | 会话观察、索引、终端与本机进程证据 |
| `session_panel/workspace` | 项目、进度、管理存储与来源观察 |
| `session_panel/execution` | 执行准入、执行记录与受控进程 |
| `session_panel/profiles` | 用户配置、导入备份与离线恢复 |
| `session_panel/integrations` | 外部接入、固定启动器与旧只读 MCP |
| `native/observer` | Windows 只读进程观察、受限本机管道及核验后的窗口返回；不写管理库 |

`session_panel/paths.py` 统一固定仓库根路径。根包的四个旧 CLI 文件只保留兼容入口，内部引用指向功能包中的真实实现，不能依赖入口转发内部符号。

前端按 `web/sessions`、`web/progress`、`web/workspace` 分组；执行界面属于工作台，放在 `web/workspace/execution.js`。新增页面资源放入所属功能目录，并在 HTTP 静态白名单中显式映射。物理路径与公开 URL 分开维护：搬文件不改变 `/workspace`、`/workspace.js` 等地址，也不改变普通脚本加载顺序。

测试按类型落位：Python 单元/合成接口测试在 `tests/backend`，纯 Node 源码与 DOM 合同在 `tests/frontend`，浏览器及协议 harness 在 `tests/e2e`，共享合成程序在 `tests/fixtures`。Python 发现入口仍是 `tests`；新增后端测试保持可递归发现。新增功能若横跨前后端，各自放入对应功能目录，并补充相应类型测试。

## 环境和验证

使用 Windows、Python 3.12 和 Node.js 24。Python 全套测试包含 Windows 原生接口；纯 Node 合同只依赖内置模块，无需根目录 npm 安装。以下创建命令仅适用于没有 `.venv` 的新检出；已有环境先确认归属和版本，不覆盖他人的环境，必要时选择独立工作树。再从仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --test tests/frontend/test_execution_ui.cjs tests/frontend/test_progress_ui.cjs tests/frontend/test_workspace_history_ui.cjs
```

基础测试使用合成配置、临时文件和 mock，不需要作者服务器或个人工具。首次完整测试涉及固定 Gitleaks 检查时，先按下文安装已核验工具；不能把缺工具跳过称为完整检查通过。本机 requirements 固定 MCP 1.30.0，远端新 reader 环境另用 requirements-reader.txt，不能把两套环境混为一次升级。

### Windows 原生观察开发

原生部分使用固定 Rust 1.97.1 x64 MSVC、rustfmt/clippy 和 Visual Studio C++ Build Tools。先在 VS Native Tools 环境进入项目。工具链首次准备是单独明确步骤，全部留在本检出 ignored `work/native-observer`，不修改宿主默认工具链或全局 PATH：

```powershell
$env:RUSTUP_HOME = Join-Path (Get-Location) 'work/native-observer/rustup-home'
$env:CARGO_HOME = Join-Path (Get-Location) 'work/native-observer/cargo-home'
$env:CARGO_TARGET_DIR = Join-Path (Get-Location) 'work/native-observer/target'
rustup set auto-self-update disable
rustup toolchain install 1.97.1-x86_64-pc-windows-msvc --profile minimal --component rustfmt --component clippy --no-self-update
```

已经准备好的检出不重复安装。普通构建入口只检查七个实际工具文件，缺失明确失败，不自动下载工具链、不回落宿主。各子进程继承固定项目 RUSTUP_HOME/CARGO_HOME/TARGET_DIR 与 RUSTUP_AUTO_INSTALL=0；项目 crate 可按 Cargo.lock 下载到项目缓存。不要手动用未隔离的 rustup/cargo 代替以下入口：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 scripts/build_observer.py fmt
.\.venv\Scripts\python.exe -B -X utf8 scripts/build_observer.py test
.\.venv\Scripts\python.exe -B -X utf8 scripts/build_observer.py clippy
.\.venv\Scripts\python.exe -B -X utf8 scripts/build_observer.py build
node --test tests/frontend/test_observation_ui.cjs
```

应用 Cargo.lock 提交源码，EXE/PDB、安装 manifest、工具链和日志均不提交。产物固定在 `work/native-observer/install`；正在使用的后台应先通过其已核验 CLI 正常停止，再替换产物。HTTP 不搜索 PATH 中同名后台，不在请求中编译。`native/observer/protocol.json`记录跨语言字段与身份测试向量，`src/protocol.rs`为严格请求解析；修改协议必须同步 Python 消费与合同测试。消息协议为v2，profile身份哈希和管道命名空间仍为v1，避免同一profile并行两套采样者。升级须先用旧固定产物正常停止自有daemon再替换，身份/协议不匹配时明确失败。登记配置v3由Python唯一管理，原生只接收有界快照；原生schema变更由单一负责人维护后交Python消费。Rust 单元测试仅用纯输入和当前测试进程，不启动现场 Agent；真实管道/后台生命周期、可见窗口及浏览器验收另由验收负责人执行并留证。

从干净检出验收正常开发时，在上述安装和测试之后，用独立目录验证初始化与示例校验：

```powershell
$checkData = Join-Path (Get-Location) 'work/new-member-profile'
.\.venv\Scripts\python.exe -m session_panel.profile init --data-dir $checkData
.\.venv\Scripts\python.exe -m session_panel.profile validate-settings --file (Resolve-Path integration-settings.example.json).Path
.\.venv\Scripts\python.exe -m session_panel.profile import-settings --file (Resolve-Path integration-settings.example.json).Path --data-dir $checkData
.\.venv\Scripts\python.exe -m session_panel.profile status --data-dir $checkData
.\.venv\Scripts\python.exe -m session_panel.gateway --data-dir $checkData --port 14401
```

页面验收负责人打开本机 14401/workspace，登记自己创建的测试项目目录，确认任务/人工记录可保存、四项高级接入显示未配置；需要 Codex 时只登记自己的库或独立合成库。停止该实例再启动同目录，确认数据保留。启动服务和浏览器人工验收分别留证，不以单元测试替代。

浏览器 harness 另需已安装 Microsoft Edge，使用 Playwright Library。仅将 npm 依赖装入 ignored work/browser，根目录不增加 npm 构建层：

```powershell
npm install --prefix work/browser playwright
$env:NODE_PATH = (Resolve-Path work/browser/node_modules).Path
node -e "console.log(require.resolve('playwright'))"
node tests/e2e/verify_session_panel.cjs --fixture
node tests/e2e/verify_execution.cjs --fixture
node tests/e2e/verify_workspace.cjs --fixture
```

三个脚本均使用普通 require('playwright') 和 Edge 的 msedge channel，合成模式不会访问私人会话或真实执行 API。verify_profile.cjs 的 protocol/http 两种模式另需独立合成数据与临时 loopback 服务，由验收负责人按其参数配置。上述测试和 CI 不证明私人环境可用；失败、警告、跳过和缺环境都应保留。

采用 [Playwright Library](https://playwright.dev/docs/library) 的已有脚本入口；不为这次迁移另建测试框架。npm 的 [prefix 安装](https://docs.npmjs.com/cli/v11/commands/npm-install/) 将依赖留在工作目录，Node 的 [NODE_PATH 查找](https://nodejs.org/api/modules.html#loading-from-the-global-folders) 仅在当前验证进程环境中设置。

清理时先列明本轮新建的临时目录和进程；仅移除已确认归属的一次性产物，保留验证日志、迁移备份和用户数据。不得递归清空整个 work/、docs/ 或 videos/，也不得清理其他任务内容。

## 协作和交接

### 管理库schema2与离线迁移验证

管理schema、profile配置和backup manifest分别版本化；schema1只能显式通过 `python -m session_panel.profile migrate-tasks --data-dir <绝对目录>` 升级。新库直接schema2；restore兼容manifest1/schema1与manifest2/schema2，旧结构恢复发布后仍须显式迁移。不得在HTTP启动、Profile.open或配置保存中隐式升级，不删除未知表或执行账本以适配schema。

任务存储的唯一写表为task_items，tasks/todos仅为保留原ID的只读视图。所有管理连接严格核schema后注册无副作用writer版本函数，固定DML guards拒旧连接误写；这是版本兼容机制，不是对恶意DB编辑的安全认证。项目写入及事项写入要在同一事务失效列表revision，不能只修改项目归档状态而接受旧分页。

迁移的SQLite一致备份在写事务外取得，EXCLUSIVE内重核源事实后逐条DDL/复制/校验/更新版本并提交，不能用executescript隐式提交。失败回滚并保留备份；回退旧程序前先 `preserve-tasks` 保全最新schema2，再恢复旧备份到新目录，不覆盖新库。备份包含私密路径和管理数据，不提交。

实现者仅运行经代码核过的无listener合成合同；真实旧连接/多进程停写、CLI迁移恢复回退、HTTP/Edge以及全量discover由验收负责人承接并保存全部退出码、失败和清理收据。纯单测入口为 `tests.backend.test_task_store` 与 `tests.backend.test_task_migration`；它们只操作TemporaryDirectory，不是用户迁移验收。

1. 任务说明记录目标、验收、负责人、分支/worktree、拟改文件和当前状态。开始、交接和结束均核对 `git status`、diff，注明阻塞与未验证项。
2. 独立任务使用独立分支或 worktree；若共用工作树，先分配文件负责人。需要改同一文件时先协调；合并冲突保留双方意图和证据。
3. PR 写明目的、范围、验证命令和退出码、风险与遗留，并指定一名未参与实施的审查人。后续修改后重新验证并请审查最终 diff。
4. 按任务授权提交和推送。使用显式文件清单暂存并检查 staged diff，推送前检查上游和完整 outgoing commit 区间。不要将别人的未提交改动混入自己的提交。

提交和推送遵循当前任务授权。不提交图片视频，也不为清理范围删除其他会话的媒体草稿。没有选定仓库许可证前，不自行添加许可证或宣称任意再分发许可。

## 内容和秘密检查

安装固定的官方 Gitleaks 8.30.1 Windows x64（下载资产 SHA-256 核验后才解压，不修改 PATH）：

```powershell
pwsh -File scripts/install_gitleaks.ps1 -Destination work/tools/gitleaks-8.30.1
python scripts/check_repository.py changes --base <base-commit> --head <head-commit> --report work/checks/changes.json
```

安装命令只运行一次，安装器拒绝覆盖已有目录。后续复用已校验工具；若选其他 ignored 工具目录，检查命令加 `--gitleaks <该目录/gitleaks.exe>`。升级须重新核实官方版本和资产校验值。

`changes` 检查 base..head 全部新增提交中逐提交新增行，个人路径或秘密先加入再删除也会阻断。普通提交对比父树；合并提交检查相对各父均新增的隐私命中，避免把同步 base 已有值当作新引入，侧支新值仍由其引入提交检查。Gitleaks 扫描同一完整区间，并显式使用 `--diff-merges=first-parent`，覆盖仅在合并解决冲突时加入的秘密。报告记录 base、head、merge_base 及每项隐私命中的 commit，兼容 PR 分支创建后 main 又前进的情况。未修改的旧个人值不阻断普通 PR；改名保持的旧内容不算新增。base/head 必须是真实提交；首次导入的全零 before、缺失 ref、无共同历史或空提交区间会失败，维护者须明确首次导入范围并运行完整候选检查，不能静默跳过。

`docs/` 开发记录和 `videos/` 制作资料只留本地；.gitignore 不会自动取消旧文件跟踪，发布负责人须核对最终跟踪清单且保留本机原件。`docs/` 在两种扫描模式均禁入；`videos/` 下已有 `.md`、`.json`、`.mjs` 文本仅在 `changes` 模式的原路径修改中进入完整扫描，仍受嵌套目录、私人文件、二进制和隐私/秘密检查约束，新增、改名或复制到该目录的内容及媒体仍禁入。

未提交文件用严格的 `release` 模式，不虚构 changes 提交范围。手写一个 ignored JSON 清单，`files` 列出本次准备发布的全部相对文件路径，`excluded` 明确记录未纳入的路径（可为空）：

```json
{"files": ["AGENTS.md", "CONTRIBUTING.md"], "excluded": []}
```

```powershell
python scripts/check_repository.py release --manifest work/checks/candidates.json --report work/checks/release.json
```

该例只是两文件候选，不是完整产品。全树候选可由 `git ls-files -z` 的路径清单生成，再明确加入待发布的未跟踪文件；先筛选禁入路径，绝不宽扫 ignored 目录。报告须列出 included/excluded/blocked；有 excluded 时结果只能说明子集。发布前必须确认模块、资源和文档闭包完整并单独检查历史边界。

检查器拒绝禁入文件、媒体、二进制、空候选、越界或链接路径；Windows 路径组件尾点/空格会被拒绝，规范长路径还会再次检查禁入规则，8.3 短名不能绕过。它不会读取禁入文件内容，仅对候选复制出的文本运行 Gitleaks，不扫描工作区私人数据。隐私规则覆盖绝对 Windows 用户/工程路径、Unix 个人 home 和普通/转义外部 IPv4，允许 loopback、文档保留地址和明确占位。该有限检查加 Gitleaks 不能证明不存在所有敏感信息。报告仅含规则、相对位置和哈希，写入 ignored `work/`；不要将原始扫描日志或报告作为公开 artifact。工具缺失、版本不匹配、读取错误或扫描失败都使检查失败。

发现真实秘密先由持有人撤销或轮换，公开记录只留类别和位置。当前树清理、当前候选、待推送提交及既有历史是四种范围；改当前文件不等于清理历史。历史改写、强推和删除分支仍须明确授权。

## CI 与维护者启用步骤

工作流使用普通 `pull_request`、main 的 `push` 和手动 `workflow_dispatch`，默认仅 `contents: read`，不持久化 checkout 凭据、不上传扫描报告。稳定状态名为 `windows-tests`、`new-content-checks`；手动 `release-readiness` 检查当前全部 Git 跟踪候选，不属于普通开发 PR 的必过项。它遇到现有媒体或旧个人值会阻塞，不能通过排除后宣称全仓库已脱敏。

维护者获得管理权限并授权发布后执行：

1. 先让 CI 分支在 GitHub 实际运行，记录 run URL 和两个状态名；本地测试和 YAML 检查不能替代远端运行。
2. 为 main 建 active ruleset：要求 PR、1 名独立批准、新 push 撤销旧批准、解决讨论，必过 `windows-tests` 与 `new-content-checks`，禁止 force push 和删除。绕过列表默认空；紧急例外须明确决定并留记录。
3. 若只有一名维护者，不能把自己批准自己的 PR 当成独立审查；应先确定协作者，或记录维护者决定的临时例外。
4. 确认真实具有 write 权限的维护者后再添加 CODEOWNERS 并启用 owner review，不编造所有者。
5. 读取 effective ruleset 和实际 PR checks，才可记录“远端门禁已启用”。目前规则文件仅为准备，远端执行与保护设置待维护者完成。

采用 GitHub 原生 [贡献指南](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/setting-guidelines-for-repository-contributors)、[分支规则](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets) 和官方 [Gitleaks CLI](https://github.com/gitleaks/gitleaks)。Actions 直接依赖固定完整 SHA；未采用仍在预览的 actions.lock，不声称具有传递依赖锁定。
