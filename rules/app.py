"""The engine's FastMCP application object, shared by the tools and the entry point."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("InfinityRolls", log_level="WARNING")
