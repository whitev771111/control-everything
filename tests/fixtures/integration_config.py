"""Independent synthetic integration settings; never discover a user environment."""


def integration_config():
    return {
        "version": 2,
        "codex": {"enabled": False, "db_path": None},
        "integrations": {
            "remote": {
                "enabled": True,
                "source": {"id": "fixture@example.invalid:/home/example/.claude",
                           "host": "example.invalid", "user": "example",
                           "config_dir": "/home/example/.claude", "claude_version": "2.1.278"},
                "home": "/home/example", "ssh": "ssh.exe",
                "reader_python": "/home/example/reader/bin/python",
                "reader_mcp_version": "1.30.0",
                "slot_files": {slot: f"/home/example/slots/{slot}.cwd" for slot in ("main", "dev", "files")},
            },
            "terminal": {
                "enabled": True, "zellij": "C:/example/tools/zellij.exe",
                "session": "fixture-session", "tab": "Fixture tab", "version": "0.45.1",
                "pane_script": "~/.local/share/fixture/pane.sh",
                "ssh_executables": ["ssh.exe", "C:/example/OpenSSH/ssh.exe"],
            },
            "workbench": {
                "enabled": True, "wezterm_gui": "C:/example/tools/wezterm-gui.exe",
                "wezterm_lua": "C:/example/config/wezterm.lua", "zellij_config": "C:/example/config/zellij.kdl",
                "workdir": "C:/example/workspace", "unmanaged_mux": "C:/example/tools/wezterm-mux-server.exe",
                "pins": {role: "a" * 64 for role in ("zellij", "wezterm_gui", "wezterm_lua", "zellij_config")},
            },
            "launchers": {
                "enabled": True, "groups": [{"id": "tools", "name": "Example tools", "blurb": "Synthetic entries"}],
                "items": [
                    {"id": "batch", "group": "tools", "name": "Batch", "blurb": "Synthetic batch",
                     "kind": "cmd", "target": "C:/example/tools/start.cmd", "probe": ""},
                    {"id": "python", "group": "tools", "name": "Python", "blurb": "Synthetic script",
                     "kind": "py", "target": "C:/example/tools/start.py", "probe": ""},
                    {"id": "task", "group": "tools", "name": "Task", "blurb": "Synthetic task",
                     "kind": "task", "target": "ExampleTask", "probe": "example.exe"},
                    {"id": "website", "group": "tools", "name": "Website", "blurb": "Synthetic URL",
                     "kind": "url", "target": "http://127.0.0.1:19999/", "probe": "http://127.0.0.1:19999/"},
                ],
            },
        },
    }
