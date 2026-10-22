"""Declarative Agent connection presets; no installation or process execution."""

CAPABILITIES = ("task.read", "task.claim", "task.progress", "task.review.submit", "heartbeat")


def _field(key, label, kind="text", *, required=False, placeholder=None, help_text=None):
    return {"key": key, "label": label, "type": kind, "required": required,
            "placeholder": placeholder, "help": help_text}


def catalog():
    return {"adapters": [
        {"id": "codex", "name": "Codex", "kind_suggestion": "codex", "transport": "builtin",
         "description": "本机 Codex CLI/桌面 Agent；命令只用于登记和发现，不启动进程。",
         "default_command": "codex", "capabilities": list(CAPABILITIES),
         "fields": [_field("command", "可执行文件/命令", placeholder="codex"),
                    _field("config_dir", "配置目录", "path", placeholder="~/.codex")],
         "setup": "在 Agent 自己的 MCP 配置中连接工作台；不要把 token 写进公开脚本。"},
        {"id": "claude-code", "name": "Claude Code", "kind_suggestion": "custom", "transport": "builtin",
         "description": "本机 Claude Code CLI；可读取会话元数据，任务联动走工作台 Agent API。",
         "default_command": "claude", "capabilities": list(CAPABILITIES),
         "fields": [_field("command", "可执行文件/命令", placeholder="claude"),
                    _field("config_dir", "配置目录", "path", placeholder="~/.claude")],
         "setup": "在 Claude Code 的 MCP 配置中加入工作台；token 由设置页生成一次。"},
        {"id": "pi", "name": "Pi", "kind_suggestion": "custom", "transport": "builtin",
         "description": "本机 Pi coding agent；按实际 CLI 或 MCP 配置接入。",
         "default_command": "pi", "capabilities": list(CAPABILITIES),
         "fields": [_field("command", "可执行文件/命令", placeholder="pi"),
                    _field("args", "启动参数", "args", placeholder="--mcp")],
         "setup": "优先使用 Agent 自己的 MCP 客户端配置；没有 MCP 时使用 CLI 包装器。"},
        {"id": "grok", "name": "Grok", "kind_suggestion": "custom", "transport": "builtin",
         "description": "本机 Grok CLI/自定义入口；没有本地入口时使用手动登记。",
         "default_command": "grok", "capabilities": list(CAPABILITIES),
         "fields": [_field("command", "可执行文件/命令", placeholder="grok"),
                    _field("endpoint", "自定义本地入口", "url", placeholder="http://127.0.0.1:...")],
         "setup": "仅登记已存在的本地入口；工作台不会安装或启动 Grok。"},
        {"id": "dsh", "name": "DeepSeek Harness", "kind_suggestion": "dsh", "transport": "builtin",
         "description": "本机 DeepSeek Harness；工作台可用 headless CLI 自动执行。",
         "default_command": "dsh", "capabilities": list(CAPABILITIES),
         "fields": [_field("command", "headless CLI 命令", placeholder="dsh")],
         "setup": "命令保持 dsh；工作台通过 DSH_CLI 或 PATH 定位已安装 CLI。"
                  "运行沿用 DSH headless 原生模型与凭据配置，包括已配置的 LiteLLM 通道；无需在工作台重复填写模型 key 或统一登录 DeepSeek。"
                  "尚未配置时，先在原生工具准备对应模型通道。登记保存、工具发现不代表推理或文件开发成功。"
                  "首次登记后若仍提示执行服务未就绪，重启工作台后复查；实际响应以本次运行为准。"},
        {"id": "mcp.stdio", "name": "自定义 MCP（stdio）", "kind_suggestion": "custom", "transport": "mcp",
         "description": "用户自己的 MCP Agent；工作台做客户端或由 Agent 连接工作台 MCP。",
         "default_command": None, "capabilities": list(CAPABILITIES),
         "fields": [_field("command", "MCP 启动命令", required=True, placeholder="python"),
                    _field("args", "参数", "args", placeholder="[\"my_agent.py\", \"--mcp\"]"),
                    _field("cwd", "工作目录", "path")],
         "setup": "命令使用 argv 数组，不经过 shell；测试连接不会执行任务。"},
        {"id": "mcp.http", "name": "自定义 MCP（HTTP）", "kind_suggestion": "custom", "transport": "mcp",
         "description": "已有本地 MCP HTTP/SSE 服务。",
         "default_command": None, "capabilities": list(CAPABILITIES),
         "fields": [_field("endpoint", "MCP URL", "url", required=True, placeholder="http://127.0.0.1:.../mcp"),
                    _field("auth_header", "认证头名称", placeholder="Authorization")],
         "setup": "只在 loopback 上暴露；不要把公网地址填进本机工作台。"},
        {"id": "cli", "name": "自定义 CLI", "kind_suggestion": "custom", "transport": "cli",
         "description": "只能执行命令的 Agent；通过工作台 CLI 包装器领取任务。",
         "default_command": None, "capabilities": list(CAPABILITIES),
         "fields": [_field("command", "CLI 命令", required=True, placeholder="my-agent"),
                    _field("args", "参数模板", "args", placeholder="task {action} {id}")],
         "setup": "建议让 Agent 调用 workbench CLI，而不是自行实现协议。"},
        {"id": "manual", "name": "手动登记", "kind_suggestion": "custom", "transport": "manual",
         "description": "不自动读写任务，只保留 Agent 身份和备注。",
         "default_command": None, "capabilities": [],
         "fields": [], "setup": "适合没有 MCP/CLI 的 Agent；任务状态由人手动更新。"},
    ]}
