from skimage.feature import canny
from skimage.color import lab2rgb, rgb2gray, rgb2lab
from scipy.ndimage import gaussian_filter
from skimage.morphology import disk
import numpy as np
import cv2
import concurrent.futures
from utils import blur, smooth
from segmenter import Segmenter

class EdgeMethod():
    def __init__(self, canny_sigma = 0.7):
        self.canny_sigma = canny_sigma
  
    def __call__(self, img):
        return canny(rgb2gray(img), sigma=self.canny_sigma).astype('float32')

def subdivide(image, n):
    """
    Splits the input image into n^2 (as equal as possible) subsections,
    even if h or w is not divisible by n.

    Args:
        image (np.ndarray): Input image (2D or 3D array).
        n (int): Number of subdivisions per axis.

    Returns:
        list: A list of n^2 image subsections (as numpy arrays).
    """
    h, w = image.shape[:2]
    h_indices = [round(i * h / n) for i in range(n + 1)]
    w_indices = [round(i * w / n) for i in range(n + 1)]
    sections = []
    for i in range(n):
        for j in range(n):
            section = image[h_indices[i]:h_indices[i+1], w_indices[j]:w_indices[j+1]]
            sections.append(section)
    return sections

def combine_sections(sections, n, original_shape=None):
    """
    Combines a list of n^2 image subsections into the original image,
    even if h or w is not divisible by n.

    Args:
        sections (list): List of n^2 numpy arrays (subsections).
        n (int): Number of subdivisions per axis.
        original_shape (tuple, optional): The original image shape (h, w) or (h, w, c).
                                        If not provided, it will be inferred.

    Returns:
        np.ndarray: The reconstructed image.
    """
    # Infer original shape if not provided
    if original_shape is None:
        # Estimate shape from sections and n
        h_total = sum([sections[i * n].shape[0] for i in range(n)])
        w_total = sum([sections[i].shape[1] for i in range(n)])
        if sections[0].ndim == 3:
            c = sections[0].shape[2]
            image = np.zeros((h_total, w_total, c), dtype=sections[0].dtype)
        else:
            image = np.zeros((h_total, w_total), dtype=sections[0].dtype)
    else:
        image = np.zeros(original_shape, dtype=sections[0].dtype)

    h_offsets = [0]
    w_offsets = [0]
    # Calculate offsets for rows
    for i in range(n):
        h_offsets.append(h_offsets[-1] + sections[i * n].shape[0])
    # Calculate offsets for columns
    for j in range(n):
        w_offsets.append(w_offsets[-1] + sections[j].shape[1])

    idx = 0
    for i in range(n):
        for j in range(n):
            h0, h1 = h_offsets[i], h_offsets[i+1]
            w0, w1 = w_offsets[j], w_offsets[j+1]
            image[h0:h1, w0:w1] = sections[idx]
            idx += 1
    return image

def normalize(img):
    img -= np.mean(img)
    return img/np.std(img)

def hessian_determinant(image, sigma=1):
    """
    Calculate the determinant of the Hessian matrix at each point in the image.
    Args:
        image: 2D numpy array (grayscale image)
        sigma: Gaussian smoothing parameter
    Returns:
        det_H: 2D numpy array of the same shape as image
    """
    # Second derivatives
    Ixx = gaussian_filter(image, sigma=sigma, order=(2, 0))
    Iyy = gaussian_filter(image, sigma=sigma, order=(0, 2))
    Ixy = gaussian_filter(image, sigma=sigma, order=(1, 1))
    # Determinant of Hessian
    det_H = Ixx * Iyy - Ixy ** 2
    return det_H

def rank_gradient(image, selem, dilate_iters = 1, erode_iters = 1):
    """
    Mimics skimage.filters.rank.gradient using OpenCV and NumPy.

    Args:
        image (np.ndarray): Grayscale input image (uint8).
        selem (np.ndarray): Structuring element (binary mask, e.g., from skimage.morphology.disk).

    Returns:
        np.ndarray: Gradient image (uint8).
    """

    # Use OpenCV to compute local min and max
    kernel = selem.astype(np.uint8)
    local_max = cv2.dilate(image, kernel, iterations=dilate_iters)
    local_min = cv2.erode(image, kernel, iterations=erode_iters)
    gradient = local_max - local_min
    return gradient


class RankGradient(EdgeMethod):
    def __init__(self, args, k=3, sigma=0, threshold=None):
        self.k = k
        self.args = args
        self.sigma = sigma
        self.threshold = threshold

    def __call__(self, seg_or_img, apply_threshold = True):
        '''
        Input 2K or 4K resolution images for best results.
        '''

        r, b, threshold, dilate_iterations = self.args

        if type(seg_or_img) is Segmenter:
            blurred = blur(seg_or_img.lab.copy(), b)
        else:
            blurred = blur(seg_or_img.copy(), b)

        footprint = np.ones((r,r))

        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = [executor.submit(rank_gradient, blurred[:,:,i], footprint, dilate_iterations, dilate_iterations) for i in range(3)]
            channel_gradients = [f.result() for f in futures]
        edges = channel_gradients[0] + channel_gradients[1] + channel_gradients[2]

        if apply_threshold:
            return edges > threshold
        else:
            return edges

class Material():
    def __init__(
        self,
        name,
        target_bg_lab,
        layer_labels,
        sigma=0.7,
        fat_threshold=0.1,
        Edge_Method = EdgeMethod,
        args = [],
        high_mag_target_bg_lab=None,
        high_mag_layer_labels=None,
    ):
        self.name = name
        self.target_bg_lab = target_bg_lab
        self.layer_labels = layer_labels
        self.high_mag_target_bg_lab = high_mag_target_bg_lab
        self.high_mag_layer_labels = high_mag_layer_labels
        self.recommended_sigma = sigma
        self.recommended_fat_threshold = fat_threshold
        self.Edge_Method = Edge_Method
        self.args = args

    def target_bg_lab_for_mag(self, magnification):
        if self.high_mag_target_bg_lab is not None and magnification >= 50:
            return self.high_mag_target_bg_lab
        return self.target_bg_lab

    def layer_labels_for_mag(self, magnification):
        if self.high_mag_layer_labels is not None and magnification >= 50:
            return self.high_mag_layer_labels
        return self.layer_labels


# Make each material

wte2_labels = { # cielab colorspace
                #   (56, 40, -10): 'monolayer',
                # #   (56, 28, -10): 'monolayer',
                #   (46, 50, -20): 'bilayer',
                #   (39, 60, -30): 'trilayer',
                #   (31, 54, -32): 'fewlayer',
                #   (70, 20, -30): 'manylayer',
                #   (80, 5, 10): 'bulk',
                #   (0, 0, 0): 'dirt',
                #   (30, 20, -10): 'dirt',
                #   (0,0,0): 'black',
                    (45.1, 42.2, -25.3): 'monolayer',
                    (35.8, 52.9, -44.4): 'bilayer',
                    (33.3, 44.1, -51.3): 'trilayer',
                    (36.4, 26.9, -53.2): 'manylayer',
                    (87.5, -21.0, 4.1): 'bulk',
                    (30, 20, -10): 'dirt',
                    (0,0,0): 'black',
              }

wte2 = Material(
    'WTe2',
    [58.50683594, 28.57762527, -2.79295444],
    layer_labels=wte2_labels,
    Edge_Method = RankGradient,
    args = [
    [2, 2, 5, 5], # Radius, Blur, Threshold, and Dilate Iterations for M50 and higher
    [2, 1.25, 5, 4], # M20
    [2, 1.5, 5, 3], # M10
    [2, 1, 5, 2] # M5
    ],
)

def clone_material(base: Material, name: str):
    return Material(
        name,
        base.target_bg_lab.copy(),
        layer_labels=base.layer_labels.copy(),
        sigma=base.recommended_sigma,
        fat_threshold=base.recommended_fat_threshold,
        Edge_Method=base.Edge_Method,
        args=[list(a) for a in base.args],
    )

# Reuse WTe2 segmentation algorithm/config exactly.
wse2 = clone_material(wte2, "WSe2")
rese2 = clone_material(wte2, "ReSe2")

graphene_labels = { # cielab colorspace
                  # (51, 14, 1): 'monolayer',
                #   (47, 12, 3): 'monolayer',
                  (46.6, 11, 4.2): 'monolayer',
                  (43.9, 18.9, 1.6): 'bilayer',
                  (40.4, 23.6, -0.6): 'trilayer',
                  (30, 20, -20): 'fewlayer',
                  (43.5, 8, 1): 'fewlayer',
                  # User-confirmed run-373 many-layer plate. The 20X image
                  # contains a large plate and a narrow strip with the same
                  # optical contrast; both belong to the many-layer target.
                  (34.4, 25.2, -19.2): 'manylayer',
                  (50, 0, -8): 'manylayer',
                  (53, 0, 0): 'manylayer',
                #   (66, -7, 11): 'manylayer',
                  (75, 0, 35): 'bulk',
                  (80, 5, 10): 'bulk',
                  (35, 1, 36): 'bulk',
                  (48, 10, 3.5): 'dirt',
                  (48, 9, 2.5): 'dirt',
                  (48.1, 6.7, -1.4): 'dirt',
                  (53, 0, 8.5): 'dirt',
                  (39, 8, 8): 'dirt',
                #   (30, 20, -10): 'dirt',
                  (0,0,0): 'black'
              }

# 100x graphene monolayers in the current microscope/camera setup cluster
# noticeably higher in a* and lower in b* than the legacy calibration.
graphene_labels_100x = {
                  (46.0, 16.2, 0.4): 'monolayer',
                  (43.9, 20.0, -1.2): 'bilayer',
                  (40.4, 23.6, -0.6): 'trilayer',
                  (30, 20, -20): 'fewlayer',
                  (43.5, 8, 1): 'fewlayer',
                  # 100X counterpart of the user-confirmed run-373
                  # many-layer plate above.
                  (24.4, 42.2, -41.5): 'manylayer',
                  (50, 0, -8): 'manylayer',
                  (53, 0, 0): 'manylayer',
                  (75, 0, 35): 'bulk',
                  (80, 5, 10): 'bulk',
                  (35, 1, 36): 'bulk',
                  (48, 10, 3.5): 'dirt',
                  (48, 9, 2.5): 'dirt',
                  (48.1, 6.7, -1.4): 'dirt',
                  (53, 0, 8.5): 'dirt',
                  (39, 8, 8): 'dirt',
                  (0,0,0): 'black'
              }

graphene = Material(
    'Graphene',
    [49.62793933, 12.17574073,  5.43972594],
    layer_labels=graphene_labels,
    high_mag_layer_labels=graphene_labels_100x,
    sigma=3,
    fat_threshold=0.005,
    Edge_Method = RankGradient,
    # args = [[1, 75], [3, 80]] # disk radius and percentile
    args = [
       [5, 3, 5, 6], # Radius, Blur, Threshold, and Dilate Iterations
       [2, 2, 5, 2],
       [4, 3, 5, 1],
       [3, 3, 4, 5],
       [4, 3, 4, 4],
       [3, 3, 5, 3],
       ],
  )

hBN_labels = { # cielab colorspace
                  (50, 20, -2): '0-10',
                  (48, 15, -9): '10-20',
                  (70.7, -31, 24.2): '20-30',
                #   (81, -41, 1): '30-40',
                  (90.7, -45, 20): '30-40',
                  # Cross-magnification calibration from run 375: the same
                  # centered hBN plate is 30-40 nm at 100X and appears more
                  # cyan at 20X after background normalization.
                  (93, -54, 53): '30-40',
                  # Thick hBN can appear as a cyan/blue terrace at 10X.  These
                  # references are calibrated from confirmed thick plates in
                  # run 368; without them, nearest-neighbor matching maps the
                  # high-b* terraces to the distant 20-30 nm reference.
                  (77, -17, 42): '40+',
                  (85, -40, 55): '40+',
                  (100, -30, 100): '40+',
                  (78, 22, -7): '40+',
                  (90, 25, 50): '40+',
                  (60, 20, 12): '40+',
                #   (70.7, -5, 24.2): '40+',
                  (30, 20, -10): 'dirt',
                #   (55, 17, 6): 'dirt',
                  (0,0,0): 'black',
              }

hBN_labels_100x = { # cielab colorspace
                  (50, 20, -2): '0-10',
                  (48, 15, -9): '10-20',
                  (56, -16, -15): '20-30',
                  (81, -41, 1): '30-40',
                  (93, -26, 18): '30-40',
                  # Confirmed 30-40 nm hBN can be green/cyan at 100X.  Keep
                  # this calibration separate from the 10X thick-blue band.
                  (93, -50, 42): '30-40',
                  (89, -43, 39): '30-40',
                  (100, -30, 30): '40+',
                  # A more saturated high-b* terrace is confirmed 40+ hBN.
                  (80, -57, 65): '40+',
                  (78, 22, -7): '40+',
                  (90, 25, 50): '40+',
                  (60, 20, -12): '40+',
                  (30, 20, -10): 'dirt',
                #   (55, 17, 6): 'dirt',
                  (0,0,0): 'black',
              }

hBN = Material(
    'hBN', 
    [49, 27.5,  8.5], 
    layer_labels=hBN_labels, 
    high_mag_layer_labels=hBN_labels_100x,
    sigma=0.7, 
    fat_threshold=0.1,
    Edge_Method=RankGradient,
    args = [
       [2, 1.5, 6, 14],
       [3, 2, 5, 3],
       [2, 2, 5, 4],
       [2, 1, 5, 1]
       ] 
  )
