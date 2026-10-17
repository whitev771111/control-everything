"""Compatibility entry point for the published module command."""

from control_everything.sessions.native_processes import main


if __name__ == "__main__":
    raise SystemExit(main())
