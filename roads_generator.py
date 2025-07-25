# roads_generator.py v3.0
# Creates a network of roads connecting locations on the map.

import random
from models import WorldState

def generate_roads_layer(world_state: WorldState) -> WorldState:
    """Creates a road network on the map grid."""
    print("[STATUS] Generating Roads Layer...")
    if not world_state.coded_map_grid:
        print("[WARNING] Coded map grid not found. Skipping road generation.")
        return world_state

    grid = [list(row) for row in world_state.coded_map_grid]
    grid_size = len(grid)

    for loc_name, location in world_state.world_map.items():
        for connection_name in location.connections:
            # A* or other pathfinding would be ideal, but for a simple grid, we can draw lines.
            # This is a simplified implementation.
            start_x, start_y = location.coordinates
            end_x, end_y = world_state.world_map[connection_name].coordinates

            # Move horizontally, then vertically
            for x in range(min(start_x, end_x), max(start_x, end_x) + 1):
                if grid[start_y][x] == '.':
                    grid[start_y][x] = '+'
            for y in range(min(start_y, end_y), max(start_y, end_y) + 1):
                if grid[y][end_x] == '.':
                    grid[y][end_x] = '+'

    world_state.roads_grid = ["".join(row) for row in grid]
    print("[STATUS] Roads Layer complete.")
    return world_state
