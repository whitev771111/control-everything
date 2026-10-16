"""Compatibility entry point for the published module command."""

from session_panel.garden.mcp import build_mcp


if __name__ == "__main__":
    build_mcp().run(transport="stdio")
