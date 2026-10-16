"""Compatibility entry point for the published module command."""

from session_panel.http.gateway import main


if __name__ == "__main__":
    raise SystemExit(main())
