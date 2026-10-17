"""Compatibility entry point for the published module command."""

from control_everything.integrations.mcp_stdio import build_mcp


if __name__ == "__main__":
    build_mcp().run(transport="stdio")
