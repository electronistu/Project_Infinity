import random
from mcp.server.fastmcp import FastMCP

# Initialize FastMCP server
mcp = FastMCP("InfinityRolls")

@mcp.tool()
def perform_check(modifier: int, dc: int, check_name: str = "Check") -> str:
    """
    Performs a D&D 5E complexity check.
    :param modifier: The bonus added to the roll.
    :param dc: The difficulty class to beat.
    :param check_name: The name of the check (e.g., 'Stealth', 'Athletics').
    """
    roll = random.randint(1, 20)
    total = roll + modifier
    
    if roll == 20:
        result = "Critical Success"
    elif roll == 1:
        result = "Critical Failure"
    elif total >= dc:
        result = "Success"
    else:
        result = "Failure"
        
    return f"{check_name}: {total} vs {dc} ({result}) ({roll} + {modifier})"

if __name__ == "__main__":
    mcp.run(transport="stdio")
