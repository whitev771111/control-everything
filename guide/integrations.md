# 可选接入

[返回项目首页](../README.md) · [使用指南](usage.md) · [可选接入](integrations.md) · [Windows 安装与维护](windows.md)

本文命令均从仓库根目录执行；安装版的专用命令另行注明。

## 配置可选接入

基础项目管理无需 SSH、Zellij、WezTerm 或 reader。已有用户也通过自己的 profile 登记这些资源；源码不提供个人服务器、工具路径、完整性摘要或启动器默认值。设置页显示每段的“未配置 / 已禁用 / 已配置”和重启提示。“已配置”仅表示通过配置校验，实际连接还会核验来源、版本和身份。

高级文件采用 version 2，包含固定四段 remote、terminal、workbench、launchers。随仓库提供的 [integration-settings.example.json](../examples/integrations/integration-settings.example.json) 四段均为 null，可直接校验。首次复制到 ignored `work/` 后编辑自己的值；已有本机配置文件时直接编辑，保留其现有内容：

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
