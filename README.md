# Control Everything · 开发工作台

在一个本地网页中管理多个项目和任务，查看开发会话、项目进度与工具用量。

适合同时使用多个开发工具、在不同项目之间切换的个人开发者。项目与任务保存在本机，基础管理无需配置 Agent、SSH 或远端服务。

[快速开始](#快速开始) · [使用指南](guide/usage.md) · [可选接入](guide/integrations.md) · [参与开发](.github/CONTRIBUTING.md)

## 产品展示



封面和视频为 `44c359f` 版本的历史演示，使用合成数据；部分入口已调整，协作小院已移除。当前能力以下表为准。

| 功能 | 可以做什么 |
| --- | --- |
| 项目管理 | 登记项目和多个工作目录，记录人工进度、查看历史，归档或恢复项目 |
| 任务中心 | 管理项目任务与待办，记录负责人和状态；分配负责人不会自动向 Agent 发指令 |
| 会话与工具观察 | 查看显式接入的 Codex 会话索引、已核验的本机工具进程及 CPU/内存；支持手动登记工具 |
| 返回工具窗口 | 在 Windows 上选择已登记工具的合格可见窗口，尝试切回继续工作；不承诺定位到终端标签页 |
| 用量与额度 | 按需查询 Codex 官方订阅额度，手动记录每日 Token、费用及其他额度 |
| AI 资讯 | 查看 Google AI、OpenAI News、Hugging Face 等固定来源，保留日期、摘要与原文入口 |
| 可选接入 | 按需配置文档进度来源、远端 Claude 会话、已有终端工作台和个人启动器 |

例如：登记项目 → 添加任务 → 更新人工进度 → 查看工具或会话状态 → 返回工具继续开发。

## 快速开始

当前提供源码启动方式。**截至 2026-10-17，公开 Releases 尚无安装包**；后续安装器及维护说明见 [Windows 安装指南](guide/windows.md)。

在 Windows 上准备 Python 3.12 或更新版本，克隆仓库后进入根目录。以下创建环境步骤适用于全新检出；已有 `.venv` 时先确认环境归属，直接复用正确的环境。

```powershell
git clone https://github.com/zxl123456123/control-everything.git
cd control-everything
python -m venv .venv
& '.venv/Scripts/python.exe' -m pip install -e .
& '.venv/Scripts/python.exe' -m pip check
& '.venv/Scripts/python.exe' -m session_panel.profile init
& '.venv/Scripts/python.exe' -m session_panel.gateway
```

打开 **http://127.0.0.1:8766/workspace**，点击“添加项目”开始使用。基础网页启动无需 Node.js 或 Rust 编译器。

默认数据保存在 `%LOCALAPPDATA%/SessionPanel`。端口被占用时退出，可通过 `--port` 指定其他端口；独立数据目录、导入与备份见[使用指南](guide/usage.md)。在启动终端按 Ctrl+C 停止网关。

## 当前边界

- 本机进程观察与窗口返回需要 Rust 原生观察后台；源码安装缺少该组件时会显示受限，基础项目管理仍可使用。准备方式见[原生观察开发](.github/CONTRIBUTING.md#windows-原生观察开发)。
- 会话身份、进程运行和人工任务进度分别展示；进程存在不代表 Agent 正在工作或任务已经完成。
- 当前不开放 Agent 自动执行、审批或模型费用试跑；远端服务和个人工具需自行配置。
- 服务只监听本机；关闭网页或 Python 网关不会结束外部 Agent，也不会停止独立观察后台。
- 项目尚未声明源码许可证；后续 Windows 技术预发布的目标平台为 Windows 10/11 x64，安装器与原生程序目前未签名。

## 详细说明

- [使用指南](guide/usage.md)：项目、任务、进度、资讯、本机观察、数据迁移与恢复。
- [可选接入](guide/integrations.md)：远端会话、终端、启动器与只读 MCP。
- [Windows 安装与维护](guide/windows.md)：安装、停止、升级、回退、卸载及发布验证。
- [参与开发](.github/CONTRIBUTING.md)：环境、目录职责、构建、测试和贡献约定。

后端按功能组织在 `src/session_panel/`，前端在 `web/`，Windows 原生观察在 `native/observer/`。`session_panel` 是现有 Python 包与启动命令的名称；新增功能放入对应功能目录。
