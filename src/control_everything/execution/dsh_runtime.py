"""DSH headless native configuration and permission policy description."""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
import threading

AUTH_MODE = "native-config"
RUNTIME_VERSION = "dsh-headless-v3"
PERMISSIONS = ("read-only", "workspace-write", "danger-full-access")
NETWORKS = ("allowed", "denied")
APPROVALS = ("request_user", "deny", "never")

_PUBLISH_LOCK = threading.Lock()


@dataclass(frozen=True)
class DshRuntimeDescription:
    overlay_path: Path
    overlay_sha256: str
    home_identity: str
    auth_mode: str
    provider: str | None
    model: str | None
    permission: str
    network: str
    approval: str
    policy_version: str

    def public(self):
        return {"auth_mode": self.auth_mode, "provider": self.provider, "model": self.model,
                "overlay_sha256": self.overlay_sha256, "home_identity": self.home_identity,
                "permission": self.permission, "network": self.network, "approval": self.approval,
                "policy_version": self.policy_version}

    def launch_args(self):
        return ("--patch", str(self.overlay_path))


def _home_identity():
    home = Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh")).resolve()
    return hashlib.sha256(os.path.normcase(str(home)).encode("utf-8")).hexdigest()


def _overlay(permission, approval):
    """Build a secret-free profile overlay; reject unsupported permission values."""
    if permission not in PERMISSIONS:
        raise ValueError("invalid_permission")
    approval_policy = "never" if permission == "danger-full-access" else (
        "ask" if approval == "request_user" else "never")
    return (
        "- id: sandbox-policy\n"
        f"  config:\n    mode: {permission}\n"
        "- id: approval\n"
        f"  config:\n    policy: {approval_policy}\n")


def build_dsh_runtime(profile, *, create=False, permission="workspace-write",
                      network="allowed", approval="request_user"):
    with _PUBLISH_LOCK:
        if permission not in PERMISSIONS or network not in NETWORKS or approval not in APPROVALS:
            raise ValueError("invalid_permission")
        effective_approval = "never" if permission == "danger-full-access" else approval
        root = (Path(profile.data_dir) / "agent-runtime" / "dsh").resolve()
        content = _overlay(permission, effective_approval)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        overlay = root / f"overlay-{digest[:16]}.patch.yml"
        if create:
            root.mkdir(parents=True, exist_ok=True)
            if overlay.is_file():
                if overlay.read_text(encoding="utf-8") != content:
                    raise ValueError("overlay_integrity_conflict")
            else:
                temporary = root / f".{overlay.name}.{os.getpid()}.{id(content)}.tmp"
                temporary.write_text(content, encoding="utf-8")
                os.replace(temporary, overlay)
                if overlay.read_text(encoding="utf-8") != content:
                    raise ValueError("overlay_integrity_conflict")
        return DshRuntimeDescription(
            overlay_path=overlay,
            overlay_sha256=digest,
            home_identity=_home_identity(),
            auth_mode=AUTH_MODE,
            provider=None,
            model=None,
            permission=permission,
            network=network,
            approval=effective_approval,
            policy_version=RUNTIME_VERSION,
        )
