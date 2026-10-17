# 架构说明

Control Everything 由浏览器界面、Python 管理后端和 Windows 原生观察组件组成。管理数据保存在本机；远端会话、外部工具和资讯按各自的接入配置访问。

## 组件与数据流

```mermaid
flowchart TD
    Browser[浏览器工作台] --> HTTP[Python HTTP 网关]
    HTTP --> Workspace[项目与任务管理]
    Workspace --> Store[本机 SQLite 管理库]
    HTTP --> Profile[配置与备份恢复]
    HTTP --> Sessions[会话与工具观察]
    Sessions --> Native[Rust Windows 原生观察组件]
    HTTP --> Integrations[可选外部接入]
```

- HTTP 网关绑定本机回环地址，处理请求校验、权限和模块装配。浏览器通过 HTTP 获取数据与静态资源。
- Python 负责项目、任务、进度和配置。跨项目与任务的关联修改在同一数据库事务中完成。
- Rust 负责 Windows 进程观察、受限本机管道和核验后的窗口返回，不写业务数据库。观察到进程存在不代表 Agent 正在执行任务。
- 源码或普通 Python wheel 缺少原生组件时，基础管理仍可使用，原生观察显示受限。完整 Windows 产品另外打包匹配的运行时和原生组件。

## 源码归属

后端采用 Python 的 `src` 布局，唯一包名为 `control_everything`。`src/` 区分可安装源码与仓库工具；包内再按功能划分，目录名不代表产品只支持会话面板。

| 路径 | 职责 |
| --- | --- |
| `src/control_everything/http` | HTTP 路由、静态资源白名单与服务装配 |
| `src/control_everything/workspace` | 项目、任务、人工进度、管理存储与资讯 |
| `src/control_everything/sessions` | 会话索引、工具进程观察与终端证据 |
| `src/control_everything/profiles` | 配置、数据目录、导入、备份与离线恢复 |
| `src/control_everything/integrations` | 外部服务、固定启动器与只读 MCP 适配 |
| `src/control_everything/execution` | 受控执行基础；当前产品不开放 Agent 执行入口 |
| `src/control_everything/installation` | Windows 产品清单、所属登记和安装维护 |
| `src/control_everything/platform` | Windows Job、句柄与安装身份原语 |
| `web/sessions`、`web/progress`、`web/workspace` | 三组页面及其脚本、样式 |
| `native/observer` | Rust 原生观察组件 |
| `tools` | 构建、开发脚本、架构检查及固定工具配置 |
| `tests/backend`、`tests/frontend`、`tests/e2e`、`tests/fixtures` | 后端、前端、端到端验证与合成数据 |

前端以普通 HTML/CSS/JavaScript 为主。观察与资讯模块由相邻 TypeScript 源码生成 JavaScript，交付时包含生成结果，普通用户启动无需 Node.js。页面主脚本负责状态和装配，完整业务展示放入所属模块。

## 依赖与扩展边界

HTTP 层可以装配各功能包，功能包不能反向依赖 HTTP。会话观察不依赖管理层；项目管理可以使用会话证据，但不调用执行或配置装配层。安装模块只由 HTTP 装配层调用，底层原语不反向依赖业务。

具体约束以 [Import Linter 配置](../tools/architecture/importlinter.ini) 为准。新增功能先选择已有职责目录；涉及前后端时分别落入各自模块，不在根目录建立第二份 Python 包或前端源码。

## 资源、数据与分发

- [paths.py](../src/control_everything/paths.py) 区分源码检出、已安装包资源与用户数据目录。运行数据不写入安装包目录。
- Web 资源只维护一份源码，构建 Python 包时按 HTTP 白名单复制。物理文件移动不能随意改变公开页面和资源 URL。
- 管理库 schema、配置格式和备份格式独立版本化；旧库通过显式命令迁移，不在 HTTP 启动时隐式升级。
- Python wheel 包含管理后端和必要 Web 资源；Windows 安装器额外组合运行时、原生观察组件和维护入口。安装器的具体操作与限制见 [Windows 安装与维护](windows.md)。

开发环境、检查命令和提交约定见[参与开发](../.github/CONTRIBUTING.md)；配置及数据恢复操作见[使用指南](usage.md)。
