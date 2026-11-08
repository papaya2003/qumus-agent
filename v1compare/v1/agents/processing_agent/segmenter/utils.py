import time
import numpy as np
import cv2
from skimage.color import lab2rgb, rgb2gray, rgb2lab
from skimage.morphology import disk

def lab_separate(img, l_factor = 1, l_midpoint=50, a_factor = 1, a_midpoint=50, b_factor = 1, b_midpoint = 50):
    lab = rgb2lab(img)
    lab[:,:,0] = lab[:,:,0] + l_factor*(lab[:,:,0] - l_midpoint*np.ones_like(lab[:,:,0]))
    lab[:,:,1] = lab[:,:,1] + a_factor*(lab[:,:,1] - a_midpoint*np.ones_like(lab[:,:,1]))
    lab[:,:,2] = lab[:,:,2] + b_factor*(lab[:,:,2] - b_midpoint*np.ones_like(lab[:,:,2]))
    over = lab[:,:,0] > 100
    under = lab[:,:,0] < 0
    lab[:,:,0][over] = 100
    lab[:,:,0][under] = 0
    return lab2rgb(lab)

def subdivide(image, n):
    """
    Splits the input image into n^2 equal subsections.

    Args:
        image (np.ndarray): Input image (2D or 3D array).
        n (int): Number of subdivisions per axis.

    Returns:
        list: A list of n^2 image subsections (as numpy arrays).
    """
    h, w = image.shape[:2]
    h_step = h // n
    w_step = w // n
    sections = []
    for i in range(n):
        for j in range(n):
            section = image[i*h_step:(i+1)*h_step, j*w_step:(j+1)*w_step]
            sections.append(section)
    return sections

def combine_sections(sections, n):
    """
    Combines a list of n^2 image subsections into the original image.

    Args:
        sections (list): List of n^2 numpy arrays (subsections).
        n (int): Number of subdivisions per axis.

    Returns:
        np.ndarray: The reconstructed image.
    """
    # Get the shape of each subsection
    h_sub, w_sub = sections[0].shape[:2]
    # Determine if image is grayscale or color
    if sections[0].ndim == 3:
        channels = sections[0].shape[2]
        image = np.zeros((n * h_sub, n * w_sub, channels), dtype=sections[0].dtype)
    else:
        image = np.zeros((n * h_sub, n * w_sub), dtype=sections[0].dtype)
    # Place each section in the correct position
    idx = 0
    for i in range(n):
        for j in range(n):
            image[i*h_sub:(i+1)*h_sub, j*w_sub:(j+1)*w_sub] = sections[idx]
            idx += 1
    return image

class Stopwatch():
    def __init__(self):
        self.prev = None

    def clock(self):
        if type(self.prev) == type(None):
            self.prev = time.time()
        else:
            current = time.time()
            print(current - self.prev)
            self.prev = current

def focus_disk(img, radius=200, invert=False):
    d = disk(radius)
    add = 0 if img.shape[0] % 2 == 0 else 1
    d = d[radius - img.shape[0]//2 : radius + img.shape[0]//2 + add, :]
    pad = np.zeros((img.shape[0], img.shape[1]//2 - radius))
    d = np.concatenate([d, pad], axis=1)
    d = np.concatenate([pad, d], axis=1)  
    d = d[:, 0:img.shape[1]]

    if invert:
        return np.logical_not(d)
    else:
        return d
    
def blur(img, sigma):
    ksize = int(2 * round(3 * sigma) + 1) if sigma > 0 else 1
    if ksize % 2 == 0:
        ksize += 1
    if sigma == 0:
        return img.copy()

    channels = []
    if len(img.shape) == 3:
        num_channels = img.shape[-1]
    else:
        num_channels = 1
    for i in range(num_channels):
        channels.append(cv2.GaussianBlur(img[:,:,i], (ksize, ksize), sigmaX=sigma, sigmaY=sigma))
    return np.stack(channels, axis=-1)

def smooth(img, radius):
    kernel = disk(radius)
    total = np.sum(kernel)
    return cv2.filter2D(img, ddepth=-1, kernel=kernel/total)

def get_mag(fname):
    if "M100" in fname or "m100" in fname:
        return 100
    elif "M50" in fname or "m50" in fname:
        return 50
    elif "M20" in fname or "m20" in fname:
        return 20
    elif "M10" in fname or "m10" in fname:
        return 10
    elif "M5" in fname or "m5" in fname:
        return 5
    else:
        return 10
    
def compute_line_distance(line_point, line_direction,X):
    """Compute distance of each point from the line"""
    # Vector from line_point to each data point
    to_points = X - line_point
    
    # Project onto line direction
    proj_lengths = (to_points @ line_direction)[:, np.newaxis]
    projections = line_point + proj_lengths * line_direction
    
    # Distance from line
    distances = np.linalg.norm(X - projections, axis=1)
    
    return distances

def compute_projection(line_point, line_direction,X):
    """Compute projection of each point onto the line"""
    # Vector from line_point to each data point
    to_points = X - line_point
    
    # Project onto line direction
    proj_lengths = (to_points @ line_direction)[:, np.newaxis]
    projections = line_point + proj_lengths * line_direction

    return np.linalg.norm(line_point - projections, axis=1)

def compute_proj_dist(line_point, line_direction,X):
    """Compute distance of each point from the line"""
    # Vector from line_point to each data point
    to_points = X - line_point
    
    # Project onto line direction
    proj_lengths = (to_points @ line_direction)[:, np.newaxis]
    projections = line_point + proj_lengths * line_direction
    
    # Distance from line
    distances = np.linalg.norm(X - projections, axis=1)
    
    return distances, proj_lengths