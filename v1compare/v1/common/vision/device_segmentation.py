"""
Device Image Segmentation Module

Provides core functionality for analyzing device images:
- Electrode detection and removal
- Material region segmentation
- Component classification
"""
import os
import cv2
import numpy as np
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import json


# Default electrode color range (HSV)
DEFAULT_ELECTRODE_RANGE = {
    'lower': np.array([15, 80, 100]),
    'upper': np.array([35, 255, 255])
}

# Material reference colors (BGR)
DEFAULT_MATERIAL_COLORS = {
    'graphite': [72, 56, 140],
    'hBN': [120, 121, 109],
    'graphite_hBN': [118, 107, 89]
}


class DeviceSegmenter:
    """
    Handles device image segmentation workflow.
    """
    
    def __init__(
        self,
        electrode_range: Optional[Dict] = None,
        material_colors: Optional[Dict] = None
    ):
        self.electrode_range = electrode_range or DEFAULT_ELECTRODE_RANGE
        self.material_colors = material_colors or DEFAULT_MATERIAL_COLORS
    
    def identify_electrodes(
        self,
        image: np.ndarray,
        min_area: int = 100
    ) -> Tuple[np.ndarray, int]:
        """
        Detect electrodes (yellow regions) in the image.
        
        Returns:
            (mask, count): Binary mask of electrodes and count
        """
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(
            hsv,
            self.electrode_range['lower'],
            self.electrode_range['upper']
        )
        
        # Morphological operations to clean up mask
        kernel = np.ones((3, 3), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)
        
        # Count electrodes
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        electrode_count = sum(1 for cnt in contours if cv2.contourArea(cnt) > min_area)
        
        return mask, electrode_count
    
    def remove_electrodes(
        self,
        image: np.ndarray,
        mask: np.ndarray,
        dilate_kernel_size: int = 45,
        scale_factor: int = 20
    ) -> np.ndarray:
        """
        Remove electrodes from image using inpainting.
        
        Args:
            image: Original BGR image
            mask: Binary mask of electrodes
            dilate_kernel_size: Size of dilation kernel
            scale_factor: Downscaling factor for faster processing
        
        Returns:
            Cleaned image (small scale)
        """
        # Expand mask to ensure complete removal
        kernel = np.ones((dilate_kernel_size, dilate_kernel_size), np.uint8)
        mask_dilated = cv2.dilate(mask, kernel, iterations=1)
        
        # Downscale for faster inpainting
        img_small = cv2.resize(
            image,
            (image.shape[1] // scale_factor, image.shape[0] // scale_factor)
        )
        mask_small = cv2.resize(
            mask_dilated,
            (img_small.shape[1], img_small.shape[0])
        )
        
        # Inpaint
        result = cv2.inpaint(img_small, mask_small, inpaintRadius=2, flags=cv2.INPAINT_TELEA)
        
        return result
    
    def detect_regions(
        self,
        image: np.ndarray,
        canny_low: int = 20,
        canny_high: int = 100
    ) -> List[np.ndarray]:
        """
        Detect material regions using edge detection and connected components.
        
        Returns:
            List of binary masks, one per region
        """
        # Bilateral filter for smoothing while preserving edges
        img = cv2.bilateralFilter(image, d=9, sigmaColor=75, sigmaSpace=75)
        
        # CLAHE for better contrast
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        L, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=2, tileGridSize=(8, 8))
        L_eq = clahe.apply(L)
        lab_eq = cv2.merge([L_eq, a, b])
        img_eq = cv2.cvtColor(lab_eq, cv2.COLOR_LAB2BGR)
        
        # Edge detection
        canny = cv2.Canny(img_eq, canny_low, canny_high)
        
        # Morphological closing to connect edges
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (4, 4))
        edges_closed = cv2.morphologyEx(canny, cv2.MORPH_CLOSE, kernel, iterations=2)
        edges = (edges_closed > 0).astype(np.uint8)
        
        # Find connected components in free space
        free = (1 - edges).astype(np.uint8) * 255
        _, labels, _, _ = cv2.connectedComponentsWithStats(free, connectivity=4)
        
        # Remove background regions (corners)
        H, W = labels.shape
        bg = {labels[1, 1], labels[1, W-2], labels[H-2, 1], labels[H-2, W-2]}
        bg.discard(0)
        for b in bg:
            labels[labels == b] = 0
        
        # Export each region as separate mask
        region_masks = [
            (labels == k).astype(np.uint8) * 255
            for k in range(1, labels.max() + 1)
        ]
        
        return region_masks
    
    def classify_material(
        self,
        bgr_color: np.ndarray
    ) -> Tuple[str, float]:
        """
        Find the closest material type based on color distance.
        
        Returns:
            (material_name, distance)
        """
        min_dist = float('inf')
        closest_material = None
        
        for material, ref_color in self.material_colors.items():
            dist = np.linalg.norm(np.array(bgr_color) - np.array(ref_color))
            if dist < min_dist:
                min_dist = dist
                closest_material = material
        
        return closest_material, min_dist
    
    def compute_region_colors(
        self,
        image: np.ndarray,
        region_masks: List[np.ndarray]
    ) -> List[np.ndarray]:
        """
        Compute average BGR color for each region.
        
        Returns:
            List of mean BGR colors
        """
        mean_colors = []
        for mask in region_masks:
            mask_bool = (mask > 0)
            pixels = image[mask_bool]
            if len(pixels) > 0:
                mean_bgr = pixels.mean(axis=0)
                mean_colors.append(mean_bgr)
            else:
                mean_colors.append(np.array([0, 0, 0]))
        return mean_colors
    
    def group_regions_by_material(
        self,
        region_masks: List[np.ndarray],
        region_colors: List[np.ndarray]
    ) -> Dict[str, List[int]]:
        """
        Group regions by material type.
        
        Returns:
            Dictionary mapping material name to list of region indices
        """
        material_regions = {'graphite': [], 'hBN': []}
        
        for i, color in enumerate(region_colors):
            material, distance = self.classify_material(color)
            
            if material == 'graphite_hBN':
                # Add to both
                material_regions['graphite'].append(i)
                material_regions['hBN'].append(i)
            else:
                if material not in material_regions:
                    material_regions[material] = []
                material_regions[material].append(i)
        
        return material_regions
    
    def merge_and_fill_masks(
        self,
        region_masks: List[np.ndarray],
        region_indices: List[int],
        fill_kernel_size: int = 7
    ) -> Optional[np.ndarray]:
        """
        Merge multiple region masks and fill gaps.
        
        Returns:
            Combined and filled mask, or None if no regions
        """
        if not region_indices:
            return None
        
        H, W = region_masks[0].shape
        combined_mask = np.zeros((H, W), dtype=np.uint8)
        
        for idx in region_indices:
            combined_mask = cv2.bitwise_or(combined_mask, region_masks[idx])
        
        # Fill gaps using morphological closing
        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (fill_kernel_size, fill_kernel_size)
        )
        filled_mask = cv2.morphologyEx(combined_mask, cv2.MORPH_CLOSE, kernel, iterations=3)
        filled_mask = cv2.morphologyEx(filled_mask, cv2.MORPH_OPEN, kernel, iterations=1)
        
        return filled_mask
    

    def analyze_full_workflow(
        self,
        image_path: str,
        output_dir: Optional[str] = None
    ) -> Dict:
        """
        Complete analysis workflow:
        1. Load image
        2. Detect electrodes
        3. Remove electrodes and inpaint
        4. Detect and classify regions
        5. Generate material masks
        
        Returns:
            Dictionary with analysis results
        """
        # Read image
        image = cv2.imread(image_path)
        if image is None:
            raise ValueError(f"Cannot load image: {image_path}")
        
        original_h, original_w = image.shape[:2]
        
        # 1. Electrode detection
        electrode_mask, electrode_count = self.identify_electrodes(image)
        
        # 2. Remove electrodes
        cleaned_image_small = self.remove_electrodes(image, electrode_mask)
        
        # SIMULATE JPEG ARTIFACTS (Match bottom_detection.py behavior)
        # bottom_detection.py saves 'electrode_removed.jpg' and reads it back before region detection.
        # This introduce compression artifacts and ensures uint8 format, which impacts Canny/Bilateral filter results.
        success, encoded_img = cv2.imencode('.jpg', cleaned_image_small)
        if success:
            cleaned_image_small = cv2.imdecode(encoded_img, cv2.IMREAD_COLOR)

        # 3. Region detection on small image
        region_masks = self.detect_regions(cleaned_image_small)
        
        # 4. Classify regions
        region_colors = self.compute_region_colors(cleaned_image_small, region_masks)
        material_groups = self.group_regions_by_material(region_masks, region_colors)
        
        # 5. Generate material masks
        material_masks = {}
        for material, indices in material_groups.items():
            if indices:
                filled_mask = self.merge_and_fill_masks(region_masks, indices)
                if filled_mask is not None and np.any(filled_mask > 0):
                    # Resize back to original size
                    filled_mask_full = cv2.resize(
                        filled_mask,
                        (original_w, original_h),
                        interpolation=cv2.INTER_NEAREST
                    )
                    material_masks[material] = filled_mask_full
        
        # Add electrode mask at full resolution
        electrode_mask_full = cv2.resize(
            electrode_mask,
            (original_w, original_h),
            interpolation=cv2.INTER_NEAREST
        )
        material_masks['electrode'] = electrode_mask_full
        
        # Save masks if output_dir provided
        if output_dir:
            output_path = Path(output_dir)
            output_path.mkdir(parents=True, exist_ok=True)
            
            for material, mask in material_masks.items():
                mask_path = output_path / f"{material}_mask.png"
                cv2.imwrite(str(mask_path), mask)
        
        # Generate components list
        components = []
        for material, mask in material_masks.items():
            # Find contours to get individual components
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            
            for idx, contour in enumerate(contours):
                area = cv2.contourArea(contour)
                if area > 100:  # Minimum area threshold
                    M = cv2.moments(contour)
                    if M['m00'] != 0:
                        cx = int(M['m10'] / M['m00'])
                        cy = int(M['m01'] / M['m00'])
                    else:
                        cx, cy = 0, 0
                
                    # Simplify contour to reduce data size (keep ~100 points max)
                    epsilon = 0.005 * cv2.arcLength(contour, True)
                    simplified_contour = cv2.approxPolyDP(contour, epsilon, True)
                    
                    # Convert contour to list of [x, y] coordinates
                    contour_points = simplified_contour.reshape(-1, 2).tolist()
                    
                    components.append({
                        'id': f"{material}_{idx}",
                        'name': f"{material.replace('_', ' ').title()} {idx + 1}",
                        'type': material,
                        'suggested_type': material,
                        'area': int(area),
                        'centroid': [int(cx), int(cy)],
                        'contour': contour_points  # Add contour coordinates
                    })
        
        return {
            'electrode_count': electrode_count,
            'components': components,
            'material_masks': material_masks,
            'original_size': (original_w, original_h)
        }


def generate_preview_image(
    original_image: np.ndarray,
    components: List[Dict],
    material_colors: Optional[Dict] = None
) -> np.ndarray:
    """
    Generate annotated preview image with component labels.
    Updated to match bottom_detection.py: Shows stacked masks by default.
    """
    # Use generate_stacked_preview logic for consistency with bottom_detection
    # We need a dummy directory or logic to handle masks, but components passed here 
    # might not have masks attached directly.
    # HOWEVER, generate_stacked_preview requires reading masks from disk, which is slow/complex.
    # Let's recreate the 'stacked mask' look purely from components if possible?
    # No, components only have contours/names. We need the masks for the full 'filled' look.
    # bottom_detection.py has 'final_stacked_segmentation.jpg' showing FILLED REGIONS.
    # Since we can't easily get masks here without disk I/O (unless passed), 
    # we will use the CONTOURS to draw filled shapes if available, simulating the stacked look.
    
    preview = original_image.copy()
    height, width = original_image.shape[:2]
    
    # Infer stack order first
    components = infer_stack_order(components)
    
    # Sort for painting (higher stack_order = bottom = painted first)
    sorted_components = sorted(components, key=lambda x: x.get('stack_order', 0), reverse=True)
    
    # Define colors (BGR) - Matching bottom_detection.py / generic
    materials_colors_map = {
        'electrode': (0, 255, 255),       # Yellow
        'graphite': (140, 56, 72),        # Purple
        'hBN': (109, 121, 120),           # Greyish
        'graphite_hBN': (89, 107, 118),   # Dark Grey
        'other': (200, 200, 200)
    }

    # Create an overlay for transparency
    overlay = preview.copy()

    for comp in sorted_components:
        comp_type = comp.get('type', 'other')
        color = materials_colors_map.get(comp_type, materials_colors_map['other'])
        
        if 'contour' in comp and comp['contour']:
            points = np.array(comp['contour'], dtype=np.int32)
            # Draw filled contour
            cv2.drawContours(overlay, [points], -1, color, -1)
            # Draw border
            cv2.drawContours(overlay, [points], -1, (255, 255, 255), 2)
            
            # Add label at centroid
            if 'centroid' in comp:
                cx, cy = comp['centroid']
                # Draw text with outline
                label = comp.get('name', '')
                # Outline
                cv2.putText(overlay, label, (cx, cy), cv2.FONT_HERSHEY_SIMPLEX, 4.0, (0,0,0), 12)
                # Main text
                cv2.putText(overlay, label, (cx, cy), cv2.FONT_HERSHEY_SIMPLEX, 4.0, (255,255,255), 4)

    # Blend overlay
    cv2.addWeighted(overlay, 0.6, preview, 0.4, 0, preview)
    
    return preview


def infer_stack_order(components: List[Dict]) -> List[Dict]:
    """
    Infer stack order based on material types.
    Default order (top to bottom): electrode > hBN > graphite > others
    
    Args:
        components: List of component dictionaries
    
    Returns:
        Components with stack_order field added (1 = top)
    """
    # Define material priority (lower number = higher in stack)
    material_priority = {
        'electrode': 1,
        'metal_contact': 1,
        'contact_electrode': 1,
        'hBN': 2,
        'graphite_hBN': 2,
        'graphite': 3,
        'WTe2': 4,
        'other': 5
    }
    
    # Group components by material type
    material_groups = {}
    for comp in components:
        mat_type = comp.get('type', comp.get('suggested_type', 'other'))
        priority = material_priority.get(mat_type, 5)
        if priority not in material_groups:
            material_groups[priority] = []
        material_groups[priority].append(comp)
    
    # Assign stack order
    stack_order = 1
    for priority in sorted(material_groups.keys()):
        for comp in material_groups[priority]:
            comp['stack_order'] = stack_order
            stack_order += 1
    
    return components


def generate_stacked_preview(original_img: np.ndarray, components: List[Dict], analysis_dir: str) -> np.ndarray:
    """
    Generate a stacked preview image based on component masks and stack order.
    
    Args:
        original_img: Original image array
        components: List of component dictionaries (must have stack_order)
        analysis_dir: Directory containing mask files
        
    Returns:
        Composite preview image
    """
    height, width = original_img.shape[:2]
    composite = np.zeros((height, width, 3), dtype=np.uint8)
    
    # Sort components by stack order (reverse order for painting: bottom to top)
    # Higher stack_order = lower in stack (painted first)
    # Lower stack_order = higher in stack (painted last)
    sorted_components = sorted(components, key=lambda x: x.get('stack_order', 0), reverse=True)
    
    # Define colors for different materials (BGR format) - Matching bottom_detection
    material_colors = {
        'electrode': (0, 255, 255),       # Yellow
        'metal_contact': (0, 255, 255),   # Yellow
        'contact_electrode': (0, 255, 255), # Yellow
        'graphite': (140, 56, 72),        # Purple
        'hBN': (109, 121, 120),           # Greyish
        'graphite_hBN': (89, 107, 118),   # Dark Grey
        'WTe2': (255, 0, 0),              # Blue
        'other': (200, 200, 200)          # Light Grey
    }
    
    import os
    import cv2
    
    for comp in sorted_components:
        # Load mask
        mask_path = os.path.join(analysis_dir, f"{comp['id']}_mask.png")
        if not os.path.exists(mask_path):
            continue
            
        mask = cv2.imread(mask_path, cv2.IMREAD_GRAYSCALE)
        if mask is None:
            continue
            
        # Resize mask to match original image if needed
        if mask.shape[:2] != (height, width):
            mask = cv2.resize(mask, (width, height), interpolation=cv2.INTER_NEAREST)
            
        # Get color
        comp_type = comp.get('type', 'other')
        color = material_colors.get(comp_type, material_colors['other'])
        
        # Apply mask to composite
        # Create colored layer for this component
        layer = np.zeros_like(composite)
        layer[mask > 0] = color
        
        # Update composite where mask is present
        mask_bool = mask > 0
        composite[mask_bool] = layer[mask_bool]
        
    return composite


