"""Compatibility entry point for the published module command."""

from control_everything.http.gateway import main


if __name__ == "__main__":
    raise SystemExit(main())
