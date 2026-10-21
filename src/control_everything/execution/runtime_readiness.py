"""Windows sandbox readiness states and explicit setup descriptions."""

WINDOWS_SANDBOX_STATES = (
    "unknown",
    "login_required",
    "windows_sandbox_setup_required",
    "windows_sandbox_unavailable",
    "ready",
)


def readiness(observed=None):
    return observed if observed in WINDOWS_SANDBOX_STATES else "unknown"


def setup_spec(runtime, executable):
    """Describe the official elevated setup; callers must not execute it implicitly."""
    return {"argv": [str(executable), "-c", 'cli_auth_credentials_store="file"',
                     "sandbox", "setup", "--elevated", "--current-user",
                     "--codex-home", str(runtime.home)],
            "home": str(runtime.home), "env": dict(runtime.child_env),
            "requires_user_confirmation": True, "sandbox_mode": runtime.windows_sandbox_mode}


def failure_reason(actual_sandbox, requested_mode="workspace-write"):
    if requested_mode == "workspace-write" and isinstance(actual_sandbox, dict) \
            and actual_sandbox.get("type") == "readOnly":
        return "windows_sandbox_setup_required"
    return "effective_policy_mismatch"


def classify_native_failure(reason):
    """Map a native helper/runner failure to a sanitized diagnostic; never persist raw text."""
    if not isinstance(reason, str):
        return None
    text = reason.casefold()
    if "runner pipe" in text and ("timed out" in text or "timeout" in text or "connect" in text):
        return {"status": "windows_sandbox_unavailable", "phase": "runner_pipe_connect",
                "code": "runner_pipe_timeout", "summary": "Windows sandbox runner pipe did not connect."}
    if "createprocesswithlogonw failed" in text:
        return {"status": "windows_sandbox_unavailable", "phase": "runner_start",
                "code": "runner_start_failed", "summary": "Windows sandbox runner process could not start."}
    if "setup refresh had errors" in text:
        return {"status": "windows_sandbox_unavailable", "phase": "runner_refresh",
                "code": "runner_refresh_failed", "summary": "Windows sandbox runner refresh failed."}
    if "native mxc is unavailable" in text:
        return {"status": "windows_sandbox_unavailable", "phase": "runner_capability",
                "code": "mxc_unavailable", "summary": "The selected native sandbox executor is unavailable."}
    return None
