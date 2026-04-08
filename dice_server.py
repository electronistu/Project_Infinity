import random
from mcp.server.fastmcp import FastMCP

# Initialize FastMCP server
mcp = FastMCP("InfinityRolls")

@mcp.tool()
def roll_d20() -> str:
    """Rolls a 20-sided die for D&D 5E checks."""
    result = random.randint(1, 20)
    return f"The d20 rolled: {result}"

if __name__ == "__main__":
    mcp.run(transport="stdio")
