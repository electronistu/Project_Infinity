import numpy as np

def _generate_numpy_noise(width, height, scale=25.0, octaves=6, persistence=0.5, lacunarity=2.0):
    """Generates smooth noise using NumPy via bilinear interpolation of random grids."""
    def get_noise_layer(w, h, s):
        grid_w, grid_h = int(w / s) + 2, int(h / s) + 2
        noise_grid = np.random.rand(grid_h, grid_w)
        
        x = np.linspace(0, grid_w - 1, w)
        y = np.linspace(0, grid_h - 1, h)
        xi, yi = np.meshgrid(x, y)
        
        x0 = np.floor(xi).astype(int)
        x1 = np.clip(x0 + 1, 0, grid_w - 1)
        y0 = np.floor(yi).astype(int)
        y1 = np.clip(y0 + 1, 0, grid_h - 1)
        
        # Bilinear interpolation
        nx0 = noise_grid[y0, x0]
        nx1 = noise_grid[y0, x1]
        ny0 = noise_grid[y1, x0]
        ny1 = noise_grid[y1, x1]
        
        tx = xi - x0
        ty = yi - y0
        
        return (1 - tx) * ((1 - ty) * nx0 + ty * ny0) + \
               tx * ((1 - ty) * nx1 + ty * ny1)

    world = np.zeros((height, width))
    amplitude = 1.0
    frequency = 1.0
    
    for _ in range(octaves):
        world += amplitude * get_noise_layer(width, height, scale / (frequency * 1.0))
        amplitude *= persistence
        frequency *= lacunarity
        
    return world

def create_map(width=50, height=50):
    """Creates a 50x50 map using NumPy noise and adds a pirate island."""
    
    # Generate NumPy noise map
    world = _generate_numpy_noise(width, height)

    # Normalize the world to be between -1 and 1 to mimic pnoise2 range
    world = (world - np.mean(world)) / np.std(world) * 0.5

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
    land_threshold = -0.6

    # Create the map grid
    grid = [['~' if world[i][j] > land_threshold else '.' for j in range(width)] for i in range(height)]

    # Create the pirate island
    island_size = 10
    island_x, island_y = width - island_size - 5, 5
    for i in range(island_y, island_y + island_size):
        for j in range(island_x, island_x + island_size):
            if i >= 0 and i < height and j >= 0 and j < width:
                if grid[i][j] == '~':
                    grid[i][j] = 'b'

    return grid
