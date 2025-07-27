import noise
import numpy as np

def create_map(width=100, height=100):
    """Creates a 100x100 map using Perlin noise and adds a pirate island."""
    
    # Perlin noise parameters
    scale = 50.0
    octaves = 6
    persistence = 0.5
    lacunarity = 2.0

    # Generate Perlin noise map
    world = np.zeros((height, width))
    for i in range(height):
        for j in range(width):
            world[i][j] = noise.pnoise2(i/scale, 
                                        j/scale, 
                                        octaves=octaves, 
                                        persistence=persistence, 
                                        lacunarity=lacunarity, 
                                        repeatx=width, 
                                        repeaty=height, 
                                        base=0)

    # Normalize the world to be between 0 and 1
    world = (world - np.min(world)) / (np.max(world) - np.min(world))

    # Create a circular gradient to form a central continent
    center_x, center_y = width // 2, height // 2
    for i in range(height):
        for j in range(width):
            dist_x = abs(i - center_x)
            dist_y = abs(j - center_y)
            dist = np.sqrt(dist_x**2 + dist_y**2)
            gradient = dist / np.sqrt(center_x**2 + center_y**2)
            world[i][j] -= gradient

    # Threshold for land vs. water
    land_threshold = -0.1

    # Create the map grid
    grid = [['.' if world[i][j] > land_threshold else '~' for j in range(width)] for i in range(height)]

    # Create the pirate island
    island_size = 20
    island_x, island_y = width - island_size - 5, 5
    for i in range(island_y, island_y + island_size):
        for j in range(island_x, island_x + island_size):
            if i >= 0 and i < height and j >= 0 and j < width:
                grid[i][j] = '.'

    return grid