"""Isolated Codex runtime description and child environment."""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

RUNTIME_VERSION = "codex-runtime-v2"
CREDENTIAL_STORE = "file"
WINDOWS_SANDBOX_MODE = "elevated"
_ALLOWED_ENV = (
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PATH", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "USERNAME", "USERDOMAIN",
    "TEMP", "TMP", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "NUMBER_OF_PROCESSORS", "OS", "PROCESSOR_ARCHITECTURE", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "ALL_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
)
_FORBIDDEN_ENV = {
    "OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_BASE_URL", "CODEX_BASE_URL",
    "CODEX_HOME", "CODEX_SQLITE_HOME", "CODEX_LOG_DIR",
}


@dataclass(frozen=True)
class RuntimeDescription:
    home: Path
    source_instance: str
    credential_store: str
    policy_version: str
    child_env: object
    login_argv: tuple
    windows_sandbox_mode: str

    def public(self):
        return {"home": str(self.home), "source_instance": self.source_instance,
                "credential_store": self.credential_store, "policy_version": self.policy_version,
                "windows_sandbox_mode": self.windows_sandbox_mode}

    def login_spec(self, executable):
        return {"argv": [str(executable), *self.login_argv], "home": str(self.home),
                "env": dict(self.child_env)}

    def setup_spec(self, executable):
        from control_everything.execution.runtime_readiness import setup_spec
        return setup_spec(self, executable)


def _environment(home):
    env = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper not in _ALLOWED_ENV or upper in _FORBIDDEN_ENV:
            continue
        if not isinstance(value, str) or "\0" in value or "\0" in key:
            continue
        env[upper] = value
    env["CODEX_HOME"] = str(home)
    env["CODEX_SQLITE_HOME"] = str(home / "sqlite")
    return env


def build_runtime(profile, *, create=False):
    home = (Path(profile.data_dir) / "agent-runtime" / "codex").resolve()
    if create:
        (home / "sqlite").mkdir(parents=True, exist_ok=True)
    source = "codex:" + hashlib.sha256(os.path.normcase(str(home)).encode("utf-8")).hexdigest()[:16]
    child_env = MappingProxyType(_environment(home))
    return RuntimeDescription(home=home, source_instance=source, credential_store=CREDENTIAL_STORE,
                              policy_version=RUNTIME_VERSION, child_env=child_env,
                              login_argv=("-c", 'cli_auth_credentials_store="file"', "login", "--device-auth"),
                              windows_sandbox_mode=WINDOWS_SANDBOX_MODE)
