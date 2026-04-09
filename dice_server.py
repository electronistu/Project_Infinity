import random
import sys
from mcp.server.fastmcp import FastMCP

# FastMCP handles logging internally, but this guarantees stderr usage
mcp = FastMCP("InfinityRolls", log_level="WARNING")

@mcp.tool()
def perform_check(modifier: int, dc: int, check_name: str = "Check") -> dict:
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
       
    # Returning a dictionary allows the AI to parse the exact variables
    # perfectly without having to read a formatted sentence.
    return {
        "check_name": check_name,
        "base_roll": roll,
        "modifier": modifier,
        "total": total,
        "dc_to_beat": dc,
        "outcome": result
    }

if __name__ == "__main__":
    mcp.run(transport="stdio")
