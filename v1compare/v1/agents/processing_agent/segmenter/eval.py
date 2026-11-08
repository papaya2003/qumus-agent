import cv2
import numpy as np
import os
from segmenter import Segmenter
from material import graphene, hBN
from tqdm import tqdm
from utils import Stopwatch, focus_disk
from scipy.ndimage import gaussian_filter
import warnings

warnings.simplefilter('ignore', UserWarning)

colors_by_layer = {
    'monolayer': np.array([0,163,255])/255.0, # Blue
    'bilayer': np.array([29,255,0])/255.0, # Green
    'trilayer': np.array([198,22,22])/255.0, # Red
    'fewlayer': np.array([255,165,0])/255.0, # Orange
    'manylayer': np.array([255,165,0])/255.0, # Orange
    'bluish_layers': np.array([255,165,0])/255.0, # Orange
    'bulk': np.array([152,7,235])/255.0, # Purple
    'dirt': np.array([0, 0, 0])/255.0, # Uncolored
    'more_bluish_layers': np.array([255, 255, 0])/255.0, # Yellow
    'bg': np.array([0, 0, 0])/255.0, # Uncolored
}

# colors_by_layer = {
#     '0-10': np.array([0,163,255])/255.0, # Blue
#     '10-20': np.array([29,255,0])/255.0, # Green
#     '20-30': np.array([198,22,22])/255.0, # Red
#     '30-40': np.array([255,165,0])/255.0, # Orange
#     '40+': np.array([152,7,235])/255.0, # Purple
#     'dirt': np.array([0, 0, 0])/255.0, # Uncolored
#     'bg': np.array([0, 0, 0])/255.0, # Uncolored
#     'black': np.array([0, 0, 0])/255.0, # Uncolored
# }

# image_path = os.listdir("../monolayerGraphene/monolayer_Graphene/")[int(sys.argv[1])]

dir = '/Users/mayanksengupta/Desktop/run_99/new_results/m_100_results'
result_dir = '/Users/mayanksengupta/Desktop/run_99/new_results/solved_m100'
files = os.listdir(dir)

monolayer_sizes = np.array([])
mono_frame_nums = np.array([])
bilayer_sizes = np.array([])
bi_frame_nums = np.array([])

grow = 2
rad = int(430*grow)
f1 = focus_disk(np.zeros((755 * grow, 1350 * grow)), rad, invert=True)
f2 = focus_disk(np.zeros((755 * grow, 1350 * grow)), rad - 10, invert=True)

def blur(img, sigma):
    r = gaussian_filter(img[:,:,0], sigma)
    g = gaussian_filter(img[:,:,1], sigma)
    b = gaussian_filter(img[:,:,2], sigma)
    return np.stack([r, g, b], axis=-1)

for filename in tqdm(files):
    # if 'M100' in filename:
    #     magnification = 100
    #     folder = 'M100'
    # elif 'M50' in filename:
    #     magnification = 50
    #     folder = 'M50'
    # elif 'M20' in filename:
    #     magnification = 20
    #     folder = 'M20'
    # elif 'M10' in filename:
    #     magnification = 10
    #     folder = 'M10'
    # elif 'M5' in filename:
    #     magnification = 5
    #     folder = 'M5'
    # else:
    #     magnification = 20
    #     folder = 'M20'

    # if 'm100' not in filename or 'segmented' in filename:
    #     continue
    if '.DS_Store' in filename:
        continue

    g1 = cv2.imread(f'{dir}/{filename}')
    try:
        g1 = cv2.cvtColor(g1, cv2.COLOR_BGR2RGB)
        # g1 = cv2.resize(g1, (int(g1.shape[1] * grow), int(g1.shape[0] * grow)))
        # g1 = blur(g1, 2)

        # Initialize Segmenter
        if 'flake' in filename and '_100' in filename:
            mag = 100
            min_area = 2000
            max_area = 10000000
        else:
            mag = 10
            min_area = 200
            max_area = 1000000
        segmenter = Segmenter(g1, graphene, colors=colors_by_layer, magnification=mag, min_area=min_area, max_area=max_area, k=3, surround_bg=True)
        segmenter.process_frame()
        result = segmenter.prettify()
        result = (255 * result).astype(np.uint8)
        result = cv2.cvtColor(result, cv2.COLOR_RGB2BGR)

        cv2.imwrite(f'{result_dir}/segmented_{filename}', result)

        # mono_size = segmenter.largest_flakes('monolayer')
        # bi_size = segmenter.largest_flakes('bilayer')
        # if 'test_' in filename:
        #     i = int(filename[filename.index('_') + 1:filename.index('.')])
        #     monolayer_sizes = np.concatenate((monolayer_sizes, mono_size))
        #     mono_frame_nums = np.concatenate((mono_frame_nums, i*np.ones_like(mono_size)))
        #     bilayer_sizes = np.concatenate((bilayer_sizes, bi_size))
        #     bi_frame_nums = np.concatenate((bi_frame_nums, i*np.ones_like(bi_size)))
    except Exception as e:
        print(e)
        print(f'{filename} corrupted')

sorted_array = np.argsort(monolayer_sizes)[::-1]
print(monolayer_sizes[sorted_array])
print(mono_frame_nums[sorted_array])

sorted_array = np.argsort(bilayer_sizes)[::-1]
print(bilayer_sizes[sorted_array])
print(bi_frame_nums[sorted_array])
