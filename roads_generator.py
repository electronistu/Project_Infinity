# roads_generator.py v3.1
# Creates a network of roads connecting locations on the map, including diagonal roads.

import random
from models import WorldState

def generate_roads_layer(world_state: WorldState) -> WorldState:
    """Creates a road network on the map grid using Bresenham's line algorithm for diagonal roads."""
    print("[STATUS] Generating Roads Layer...")
    if not world_state.coded_map_grid:
        print("[WARNING] Coded map grid not found. Skipping road generation.")
        return world_state

    grid = [list(row) for row in world_state.coded_map_grid]
    grid_size = len(grid)
    processed_connections = set()

    for loc_name, location in world_state.world_map.items():
        if not location.coordinates:
            continue

        for connection_name in location.connections:
            connection_pair = tuple(sorted((loc_name, connection_name)))
            if connection_pair in processed_connections:
                continue

            connection = world_state.world_map.get(connection_name)
            if not connection or not connection.coordinates:
                continue

            # Start/end roads from the center of the location for a cleaner look
            start_x = location.coordinates[0] + location.size[0] // 2
            start_y = location.coordinates[1] + location.size[1] // 2
            end_x = connection.coordinates[0] + connection.size[0] // 2
            end_y = connection.coordinates[1] + connection.size[1] // 2

            # Bresenham's Line Algorithm for drawing roads
            dx = abs(end_x - start_x)
            sx = 1 if start_x < end_x else -1
            dy = -abs(end_y - start_y)
            sy = 1 if start_y < end_y else -1
            err = dx + dy

            x, y = start_x, start_y
            while True:
                if 0 <= y < grid_size and 0 <= x < grid_size:
                    # Draw road '+' only on land '.' or ocean '~', not over locations
                    if grid[y][x] == '.' or grid[y][x] == '~':
                        grid[y][x] = '+'
                
                if x == end_x and y == end_y:
                    break
                
                e2 = 2 * err
                if e2 >= dy:
                    err += dy
                    x += sx
                if e2 <= dx:
                    err += dx
                    y += sy
            
            processed_connections.add(connection_pair)

    world_state.roads_grid = ["".join(row) for row in grid]
    print("[STATUS] Roads Layer complete.")
    return world_state
