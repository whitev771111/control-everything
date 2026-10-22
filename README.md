# Control Everything · 开发工作台

在一个本地网页中管理多个项目和任务，查看开发会话、项目进度与工具用量。

适合同时使用多个开发工具、在不同项目之间切换的个人开发者。项目与任务保存在本机，基础管理无需配置 Agent、SSH 或远端服务。

[下载安装包](https://github.com/zxl123456123/control-everything/releases/tag/v0.1.0a1) · [项目文档](docs/README.md) · [参与开发](.github/CONTRIBUTING.md)

## 产品展示

https://github.com/user-attachments/assets/be96cc58-6bd8-4221-b0aa-77c0f9e31804

| 功能 | 可以做什么 |
| --- | --- |
| 项目管理 | 登记项目和多个工作目录，记录人工进度、查看历史，归档或恢复项目 |
| 任务中心 | 管理项目任务与待办，记录负责人和状态；分配负责人不会自动向 Agent 发指令 |
| 会话与工具观察 | 选择 Codex、Claude Code、OpenCode 的本机项目；确认来源后加入工作台，支持其他 EXE 手工登记，进程指标收在诊断 |
| 返回工具窗口 | 在 Windows 上选择已登记工具的合格可见窗口，尝试切回继续工作；不承诺定位到终端标签页 |
| 用量与额度 | 按需读取 Codex 官方额度，查看各窗口剩余比例与重置时间 |
| AI 资讯 | 查看 Google AI、OpenAI News、Hugging Face 等固定来源，保留日期、摘要与原文入口 |
| 可选接入 | 按需配置文档进度来源、远端 Claude 会话、已有终端工作台和个人启动器 |

例如：登记项目 → 添加任务 → 更新人工进度 → 查看工具或会话状态 → 返回工具继续开发。

源码开发版现提供 Codex / DSH 最小执行闭环：选择项目与 Agent → 明确执行 → 处理具体工具审批 → 人工审核 → 填写驳回理由后继续修改。使用条件及尚未解决的 Windows 原生写入问题见[最小执行流程](docs/usage.md#双-agent-最小执行流程源码开发版)；下面已有安装包尚未随本次源码更新重新发布。

## 快速开始

**Windows x64 开发预览版 0.1.0a1**：[下载安装包](https://github.com/zxl123456123/control-everything/releases/download/v0.1.0a1/session-panel-0.1.0a1-windows-x64.exe) · [版本说明与校验值](https://github.com/zxl123456123/control-everything/releases/tag/v0.1.0a1)。安装包自带运行环境，无需另装 Python、Node.js 或 Rust。

1. 下载并运行安装器。
2. 从开始菜单的 **Session Panel** 启动程序（安装入口暂沿用此名称）。
3. 打开 **http://127.0.0.1:8766/workspace**，点击“添加项目”开始使用。

也可按[源码启动指南](docs/usage.md#多-agent-工作台)运行。数据保存在本机；安装维护见 [Windows 安装指南](docs/windows.md)，独立数据目录、端口设置、导入与备份见[使用指南](docs/usage.md)。

## 当前边界

- 本机进程观察与窗口返回需要 Rust 原生观察后台；源码安装缺少该组件时会显示受限，基础项目管理仍可使用。准备方式见[原生观察开发](.github/CONTRIBUTING.md#windows-原生观察开发)。
- 会话身份、进程运行和人工任务进度分别展示；进程存在不代表 Agent 正在工作或任务已经完成。
- 源码开发版提供需明确确认的 Codex / DSH 执行与人工审核；现有安装包尚未包含该流程，完整隔离范围与已知限制见使用指南。远端服务和个人工具需自行配置。
- 服务只监听本机；关闭网页或 Python 网关不会结束外部 Agent，也不会停止独立观察后台。
- 当前为开发预览版，目标平台 Windows 10/11 x64；安装器与原生程序未签名，已做本机安装验证，尚未完成干净 Win10/Win11 环境矩阵测试。

## 详细说明

- [架构说明](docs/architecture.md)：组件职责、目录归属、数据流与扩展边界。
- [使用指南](docs/usage.md)：项目、任务、进度、资讯、本机观察、数据迁移与恢复。
- [可选接入](docs/integrations.md)：远端会话、终端、启动器与只读 MCP。
- [Windows 安装与维护](docs/windows.md)：安装、停止、升级、回退、卸载及发布验证。
- [参与开发](.github/CONTRIBUTING.md)：环境、目录职责、构建、测试和贡献约定。

## 许可证

项目源码与文档采用 [MIT 许可证](LICENSE)。第三方依赖遵循各自的许可证。
