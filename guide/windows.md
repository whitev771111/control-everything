# Windows 安装与维护

[返回项目首页](../README.md) · [使用指南](usage.md) · [可选接入](integrations.md) · [Windows 安装与维护](windows.md)

本文命令均从仓库根目录执行；安装版的专用命令另行注明。

## Windows 标准安装

截至 2026-10-17，公开 Releases 尚无安装包。以下说明适用于后续实际发布的安装器；当前请按[源码启动](../README.md#快速开始)使用。

安装器发布后，普通用户从 [GitHub Releases](https://github.com/zxl123456123/control-everything/releases) 选择 Windows x64 技术预发布的 `session-panel-<版本>-windows-x64.exe`，先用 `Get-FileHash -Algorithm SHA256` 与该次发布的 `SHA256SUMS.txt` 核对，再运行安装器。标准产品自带 CPython 3.13.16、固定 Python 依赖、前端资源和 release Rust 原生观察后台，无需另外安装 Python、Node、npm 或 Rust 编译器。wheel 和源码归档供开发使用，不能代替完整安装器。

安装目录支持中文和空格；当前产品按普通 Windows 路径工作，每个完整交付文件路径须少于260个UTF-16字符。安装器在写入目标前检查整个固定文件清单，超限时提示选择更短目录，不修改系统的长路径策略。

安装按当前用户运行，无需管理员权限，默认位置为 `%LOCALAPPDATA%/Programs/SessionPanel`。从开始菜单的 **Session Panel** 打开网关控制台，再在浏览器访问 `http://127.0.0.1:8766/workspace`；安装不会自动启动网关或外部 Agent。端口被占用时明确退出，可从 PowerShell 指定自己的端口和 profile：

```powershell
$panel = Join-Path $env:LOCALAPPDATA 'Programs/SessionPanel/session-panel.cmd'
& $panel --data-dir 'C:/example/SessionPanel/main' --port 14401
```

在所属控制台按 Ctrl+C 正常停止网关。关闭页面或网关不会终止独立原生观察后台，更不会终止外部 Agent。维护前先停止自己运行的全部网关；安装器会核对本安装登记的全部版本及 profile，并通过每个后台原来的 EXE 正常停止自有观察进程。未知身份、仍运行的 HTTP 或无法证实退出的进程会阻止维护，保留现有入口，不按程序名强杀，不接管其他安装或 profile。

升级保留旧版本目录，安装文件核验和登记完成后才切换 `session-panel.cmd`。重新运行可信的原版本安装器可选择该版本的完整闭包；同版本可重新安装。回退前保留 profile 备份，并确认旧程序支持当前配置和管理库格式；安装器不会隐式迁移、降级或搬迁数据。卸载使用 Windows“已安装的应用”中的 **Session Panel**，只处理安装日志和严格核验的自有管理文件；profile、备份和未知用户文件保留。卸载不会替你关闭网关，遇到阻塞先正常退出自己运行的实例，再重试。

目标平台为 Windows 10/11 x64；技术预发布的安装器与原生程序目前未签名，上游组件的数字签名不代表组合产品已签名。每次发布说明列出实际验收范围；开发机或托管 Windows runner 的受控 PATH 验收不能替代两种系统的完整干净机器验证。项目尚未声明源码许可证，不据此承诺任何开源许可；随产品附带实际依赖的第三方声明及摘要。

## 构建与技术预发布

产品版本只定义于 `pyproject.toml`；wheel 元数据、产品闭包和安装器显示版本消费同一个值，Inno 文件数值版本按固定映射生成。`tools/packaging/downloads.json` 固定官方 CPython 和 Inno Setup 下载及 SHA-256，`windows-cp313.lock` 固定 Windows x64/CPython 3.13 的全部轮子，`rust-toolchain.toml` 固定 Rust。前端只有 `web` 这一份手写源码，wheel 构建按资源白名单收集；EXE、依赖轮子、安装器及真实用户资料不提交 Git。

维护者使用 `.github/workflows/release.yml` 的手动入口，提交完整的已合入 main 的 40 位 commit SHA。流水线先检查该提交的 `windows-tests`、`architecture-boundaries`、`new-content-checks` 三项成功，再构建 sdist→wheel、静态 CRT release observer、严格 `--release` 产品闭包和 Inno 安装器。受测产物与源码提交、源码输入哈希、依赖锁、第三方声明及公开资产 SHA-256 绑定；独立发布 job 才获得写权限，发布 `v<版本>` 的技术 prerelease，不移动已有版本 tag。Actions artifact 上传与 GitHub Release 发布是两个独立结果，最终以 Release URL 和发布状态为准。

安装验收入口只接受全新证据目录、合成 profile 和显式产品文件；存在同 AppId 安装或已有开始菜单入口时拒绝运行。它会真实安装、运行本次隔离实例并卸载，失败后保留证据和安装，不自动强杀或删除。以下命令由验收负责人在专用 Windows 用户环境执行，路径均为自己准备的绝对路径：

```powershell
python tests/e2e/verify_installed_product.py --payload 'C:/example/build/payload' --installer 'C:/example/build/session-panel-0.1.0a1-windows-x64.exe' --evidence 'C:/example/acceptance/new-run'
# Add a different, real second closure to exercise upgrade and rollback:
python tests/e2e/verify_installed_product.py --payload 'C:/example/build-a/payload' --installer 'C:/example/build-a/setup.exe' --second-payload 'C:/example/build-b/payload' --second-installer 'C:/example/build-b/setup.exe' --evidence 'C:/example/acceptance/new-two-version-run'
```

报告区分当前机器受控 PATH、14 个资源、运行时/原生状态、HTTP 维护阻塞、两个 profile、资料及备份保留等实际结果。第二组版本、真实资讯 Job 生命周期、桌面返回、可选 SSH、真实工具及干净 Win10/Win11 验证分别留证；某一项成功不能代替其他项。公开发布文件只取明确白名单，合成验收日志留在单独短期 Actions artifact，真实个人数据不上传。
