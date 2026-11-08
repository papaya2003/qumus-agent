from __future__ import annotations

import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

try:  # pragma: no cover - optional dependency guard
    import cv2  # type: ignore
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore

try:  # pragma: no cover - optional dependency guard
    import pyboof as pb  # type: ignore
except ImportError:  # pragma: no cover
    pb = None  # type: ignore

from PIL import Image, ImageEnhance, ImageFilter

from .stage_geometry import calibrate_px_to_mm_from_qr_markers


def annotate_qr_messages(
    img_gray: np.ndarray,
    detections: Sequence[Mapping[str, Any]],
    *,
    detector: Any | None = None,
) -> None:
    if pb is None or cv2 is None:
        return
    qr_detections = [det for det in detections if _is_table_qr_detection(det)]
    if not qr_detections:
        return
    try:
        local_detector = detector or pb.FactoryFiducial(np.uint8).microqr()
    except Exception as exc:
        print(f"[Calib] QR detector unavailable: {exc}")
        return
    height, width = img_gray.shape[:2]
    for idx, det in enumerate(qr_detections, 1):
        bbox = _detection_bounds(det, width, height)
        if bbox is None:
            continue
        message = read_micro_qr_exhaustive(img_gray, bbox, idx, local_detector)
        if message and message != "Not found":
            det["qr_message"] = message.strip()


def _is_table_qr_detection(det: Mapping[str, Any], *, source: str = "left_yolo") -> bool:
    """Check if detection is a table QR marker (source-aware).
    
    Left/Right YOLO: class 5 = table micro qr
    """
    label = str(det.get("class") or det.get("label") or "").lower()
    class_id = det.get("class_id")
    if "table micro qr" in label:
        return True
    if class_id is not None:
        cid = int(class_id)
        return cid == 5
    return False


def _detection_bounds(det: Mapping[str, Any], width: int, height: int) -> tuple[int, int, int, int] | None:
    coords = det.get("xyxyxyxy")
    if coords:
        pts = np.array(coords, dtype=float).reshape(-1, 2)
        x1 = max(0, int(np.floor(np.min(pts[:, 0]))))
        y1 = max(0, int(np.floor(np.min(pts[:, 1]))))
        x2 = min(width, int(np.ceil(np.max(pts[:, 0]))))
        y2 = min(height, int(np.ceil(np.max(pts[:, 1]))))
    else:
        cx = float(det.get("x", 0.0))
        cy = float(det.get("y", 0.0))
        w = float(det.get("w", 0.0))
        h = float(det.get("h", 0.0))
        x1 = max(0, int(np.floor(cx - 0.5 * w)))
        y1 = max(0, int(np.floor(cy - 0.5 * h)))
        x2 = min(width, int(np.ceil(cx + 0.5 * w)))
        y2 = min(height, int(np.ceil(cy + 0.5 * h)))
    if x2 - x1 <= 4 or y2 - y1 <= 4:
        return None
    return (x1, y1, x2, y2)


def apply_deblur_sharpen(img_array: np.ndarray) -> np.ndarray:
    if cv2 is None:
        raise RuntimeError("apply_deblur_sharpen requires OpenCV")
    kernel_sharpen = (
        np.array(
            [
                [-1, -1, -1, -1, -1],
                [-1, 2, 2, 2, -1],
                [-1, 2, 8, 2, -1],
                [-1, 2, 2, 2, -1],
                [-1, -1, -1, -1, -1],
            ],
            dtype=float,
        )
        / 8.0
    )
    sharpened = cv2.filter2D(img_array, -1, kernel_sharpen)
    return cv2.convertScaleAbs(sharpened, alpha=1.5, beta=20)


def apply_extreme_sharpen(img_array: np.ndarray) -> np.ndarray:
    if cv2 is None:
        raise RuntimeError("apply_extreme_sharpen requires OpenCV")
    kernel = np.array(
        [
            [-1, -1, -1],
            [-1, 9, -1],
            [-1, -1, -1],
        ],
        dtype=float,
    )
    sharpened = cv2.filter2D(img_array, -1, kernel)
    return cv2.convertScaleAbs(sharpened, alpha=1.3, beta=10)


def apply_clahe(img_array: np.ndarray) -> np.ndarray:
    if cv2 is None:
        raise RuntimeError("apply_clahe requires OpenCV")
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    return clahe.apply(img_array)


def apply_morphological_cleaning(img_array: np.ndarray) -> np.ndarray:
    if cv2 is None:
        raise RuntimeError("apply_morphological_cleaning requires OpenCV")
    binary = cv2.adaptiveThreshold(
        img_array,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        11,
        2,
    )
    kernel = np.ones((2, 2), np.uint8)
    cleaned = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    return cv2.morphologyEx(cleaned, cv2.MORPH_OPEN, kernel)


def extract_and_enhance_crop(
    img: np.ndarray,
    bbox: tuple[int, int, int, int],
    scale: int = 3,
    padding: int = 30,
    enhancement_style: str = "standard",
) -> tuple[np.ndarray, tuple[int, int], int]:
    x1, y1, x2, y2 = bbox
    h_img, w_img = img.shape if len(img.shape) == 2 else img.shape[:2]
    x1_pad = max(0, x1 - padding)
    y1_pad = max(0, y1 - padding)
    x2_pad = min(w_img, x2 + padding)
    y2_pad = min(h_img, y2 + padding)
    crop = img[y1_pad:y2_pad, x1_pad:x2_pad]
    pil_crop = Image.fromarray(crop)
    new_size = (max(1, pil_crop.width * scale), max(1, pil_crop.height * scale))
    pil_crop = pil_crop.resize(new_size, Image.LANCZOS)
    if enhancement_style == "aggressive":
        pil_crop = ImageEnhance.Sharpness(pil_crop).enhance(3.5)
        pil_crop = ImageEnhance.Contrast(pil_crop).enhance(2.2)
        pil_crop = pil_crop.filter(ImageFilter.UnsharpMask(radius=3, percent=200, threshold=2))
    elif enhancement_style == "light":
        pil_crop = ImageEnhance.Sharpness(pil_crop).enhance(1.8)
        pil_crop = ImageEnhance.Contrast(pil_crop).enhance(1.5)
    else:
        pil_crop = ImageEnhance.Sharpness(pil_crop).enhance(2.5)
        pil_crop = ImageEnhance.Contrast(pil_crop).enhance(1.8)
        pil_crop = pil_crop.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
    return np.array(pil_crop), (x1_pad, y1_pad), scale


def _generate_region_variants(
    img_gray: np.ndarray,
    bbox: tuple[int, int, int, int],
    *,
    base_padding: int = 40,
) -> list[tuple[np.ndarray, int]]:
    if img_gray.size == 0:
        return []
    x1, y1, x2, y2 = bbox
    h, w = img_gray.shape[:2]
    x_min = max(0, int(np.floor(min(x1, x2))))
    x_max = min(w, int(np.ceil(max(x1, x2))))
    y_min = max(0, int(np.floor(min(y1, y2))))
    y_max = min(h, int(np.ceil(max(y1, y2))))
    if x_max - x_min < 4 or y_max - y_min < 4:
        return []
    paddings = [0, base_padding, base_padding + 30, base_padding + 60]
    variants: list[tuple[np.ndarray, int]] = []
    for pad in paddings:
        pad_x_min = max(0, x_min - pad)
        pad_y_min = max(0, y_min - pad)
        pad_x_max = min(w, x_max + pad)
        pad_y_max = min(h, y_max + pad)
        if pad_x_max - pad_x_min < 4 or pad_y_max - pad_y_min < 4:
            continue
        region = img_gray[pad_y_min:pad_y_max, pad_x_min:pad_x_max]
        if region.size == 0:
            continue
        variants.append((region.copy(), pad))
    return variants


def _apply_preprocessing_methods(region: np.ndarray) -> list[tuple[str, np.ndarray]]:
    if cv2 is None:
        return [("original", region)]
    region = np.ascontiguousarray(region)
    if region.ndim == 3:
        region = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    if region.dtype != np.uint8:
        region = np.clip(region, 0, 255).astype(np.uint8)
    methods: list[tuple[str, np.ndarray]] = []

    def add(name: str, img: np.ndarray | None) -> None:
        if img is None:
            return
        arr = np.ascontiguousarray(img)
        if arr.dtype != np.uint8:
            arr = np.clip(arr, 0, 255).astype(np.uint8)
        if arr.size == 0:
            return
        methods.append((name, arr))

    add("original", region)

    for scale in [1.5, 2.0, 2.5]:
        try:
            scaled = cv2.resize(region, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        except cv2.error:
            continue
        add(f"scaled_{scale}", scaled)
        kernel = np.array([[-1, -1, -1], [-1, 9, -1], [-1, -1, -1]], dtype=float)
        sharpened = cv2.filter2D(scaled, -1, kernel)
        add(f"scaled_{scale}_sharpen", sharpened)

    add("normalized", cv2.normalize(region, None, 0, 255, cv2.NORM_MINMAX))

    try:
        p2, p98 = np.percentile(region, (2, 98))
    except IndexError:
        p2, p98 = 0.0, 255.0
    denom = max(1.0, float(p98 - p2))
    stretched = np.clip((region.astype(np.float32) - p2) * 255.0 / denom, 0, 255)
    stretched = stretched.astype(np.uint8)
    add("percentile_stretch", stretched)
    clahe_default = cv2.createCLAHE(clipLimit=4.0, tileGridSize=(8, 8))
    add("percentile_stretch_clahe", clahe_default.apply(stretched))

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    add("morph_close_glare", cv2.morphologyEx(region, cv2.MORPH_CLOSE, kernel))

    _, glare_mask = cv2.threshold(region, 200, 255, cv2.THRESH_BINARY)
    if np.count_nonzero(glare_mask) > 0:
        inpainted = cv2.inpaint(region, glare_mask, 3, cv2.INPAINT_TELEA)
        add("inpaint_glare", inpainted)
        add("inpaint_glare_clahe", clahe_default.apply(inpainted))

    for threshold in [180, 200, 220]:
        highlight_reduced = np.minimum(region, threshold).astype(np.uint8)
        add(f"highlight_cap_{threshold}", highlight_reduced)
        add(f"highlight_cap_{threshold}_clahe", clahe_default.apply(highlight_reduced))

    region_float = region.astype(np.float32) + 1.0
    log_region = np.log(region_float)
    log_region_blur = cv2.GaussianBlur(log_region, (21, 21), 0)
    homomorphic = np.exp(log_region - log_region_blur)
    homomorphic = cv2.normalize(homomorphic, None, 0, 255, cv2.NORM_MINMAX)
    add("homomorphic", homomorphic.astype(np.uint8))
    add("homomorphic_clahe", clahe_default.apply(homomorphic.astype(np.uint8)))

    for clip, tile in [(2.0, (8, 8)), (4.0, (8, 8)), (6.0, (4, 4)), (10.0, (4, 4)), (15.0, (2, 2)), (20.0, (2, 2))]:
        clahe = cv2.createCLAHE(clipLimit=clip, tileGridSize=tile)
        add(f"clahe_clip{clip}_tile{tile}", clahe.apply(region))

    add("equalized", cv2.equalizeHist(region))

    for d, sigma in [(9, 75), (15, 100), (9, 50)]:
        try:
            bilateral = cv2.bilateralFilter(region, d, sigma, sigma)
        except cv2.error:
            continue
        add(f"bilateral_d{d}_s{sigma}", bilateral)
        clahe = cv2.createCLAHE(clipLimit=4.0, tileGridSize=(8, 8))
        add(f"bilateral_d{d}_s{sigma}_clahe", clahe.apply(bilateral))

    for ksize in [3, 5, 7]:
        try:
            blurred = cv2.GaussianBlur(region, (ksize, ksize), 0)
        except cv2.error:
            continue
        add(f"gaussian_{ksize}", blurred)
        _, otsu_blur = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        add(f"gaussian_{ksize}_otsu", otsu_blur)

    for block_size in [11, 15, 21, 31, 41]:
        for c in [2, 5, 10]:
            try:
                adaptive_gauss = cv2.adaptiveThreshold(
                    region, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block_size, c
                )
                add(f"adaptive_gauss_b{block_size}_c{c}", adaptive_gauss)
                adaptive_mean = cv2.adaptiveThreshold(
                    region, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, block_size, c
                )
                add(f"adaptive_mean_b{block_size}_c{c}", adaptive_mean)
            except cv2.error:
                continue

    _, otsu = cv2.threshold(region, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    add("otsu", otsu)
    _, otsu_inv = cv2.threshold(region, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    add("otsu_inv", otsu_inv)

    inverted = cv2.bitwise_not(region)
    add("inverted", inverted)
    for clip in [4.0, 10.0]:
        clahe = cv2.createCLAHE(clipLimit=clip, tileGridSize=(8, 8))
        add(f"inverted_clahe{clip}", clahe.apply(inverted))

    for alpha in [0.5, 0.7, 0.9, 1.1, 1.3, 1.5, 1.8, 2.0]:
        for beta in [-30, -20, -10, 0, 10, 20, 30]:
            adjusted = cv2.convertScaleAbs(region, alpha=alpha, beta=beta)
            add(f"bright_a{alpha}_b{beta}", adjusted)

    for gamma in [0.3, 0.5, 0.7, 1.0, 1.3, 1.5, 1.8, 2.0, 2.5]:
        look_up = np.empty((1, 256), np.uint8)
        for k in range(256):
            look_up[0, k] = np.clip((k / 255.0) ** gamma * 255.0, 0, 255)
        gamma_corrected = cv2.LUT(region, look_up)
        add(f"gamma_{gamma}", gamma_corrected)

    for strength in [1, 2]:
        kernel_sharp = np.array([[-1, -1, -1], [-1, 8 + strength, -1], [-1, -1, -1]], dtype=float)
        sharpened = cv2.filter2D(region, -1, kernel_sharp)
        add(f"sharpened_{strength}", sharpened)

    for ksize in [(3, 3), (5, 5)]:
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, ksize)
        add(f"tophat_{ksize}", cv2.morphologyEx(region, cv2.MORPH_TOPHAT, kernel))
        add(f"blackhat_{ksize}", cv2.morphologyEx(region, cv2.MORPH_BLACKHAT, kernel))
        add(f"gradient_{ksize}", cv2.morphologyEx(region, cv2.MORPH_GRADIENT, kernel))
        add(f"opening_{ksize}", cv2.morphologyEx(region, cv2.MORPH_OPEN, kernel))
        add(f"closing_{ksize}", cv2.morphologyEx(region, cv2.MORPH_CLOSE, kernel))

    for h_val in [10, 15, 20]:
        try:
            denoised = cv2.fastNlMeansDenoising(region, h=h_val)
        except cv2.error:
            continue
        add(f"denoised_h{h_val}", denoised)
        add(f"denoised_h{h_val}_clahe", clahe_default.apply(denoised))

    laplacian = cv2.Laplacian(region, cv2.CV_64F)
    laplacian = np.uint8(np.absolute(laplacian))
    add("laplacian", laplacian)

    gaussian = cv2.GaussianBlur(region, (9, 9), 10.0)
    unsharp = cv2.addWeighted(region, 1.5, gaussian, -0.5, 0)
    add("unsharp", unsharp)

    kernel_small = np.ones((3, 3), np.uint8)
    add("dilated", cv2.dilate(region, kernel_small, iterations=1))
    add("eroded", cv2.erode(region, kernel_small, iterations=1))

    for ksize in [3, 5, 7]:
        try:
            median = cv2.medianBlur(region, ksize)
        except cv2.error:
            continue
        add(f"median_{ksize}", median)
        add(f"median_{ksize}_clahe", clahe_default.apply(median))

    try:
        denoised = cv2.fastNlMeansDenoising(region, h=10)
        normalized_denoised = cv2.normalize(denoised, None, 0, 255, cv2.NORM_MINMAX)
        add("denoise_normalize", normalized_denoised)
        add("denoise_normalize_clahe", clahe_default.apply(normalized_denoised))
    except cv2.error:
        pass

    try:
        bilateral = cv2.bilateralFilter(region, 9, 75, 75)
        normalized_bilateral = cv2.normalize(bilateral, None, 0, 255, cv2.NORM_MINMAX)
        add("bilateral_normalize", normalized_bilateral)
        _, bilateral_otsu = cv2.threshold(
            normalized_bilateral, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )
        add("bilateral_normalize_otsu", bilateral_otsu)
    except cv2.error:
        pass

    for alpha in [2.5, 3.0, 3.5]:
        for beta in [-50, -40, 40, 50]:
            extreme = cv2.convertScaleAbs(region, alpha=alpha, beta=beta)
            add(f"extreme_a{alpha}_b{beta}", extreme)

    h, w = region.shape[:2]
    center = (w // 2, h // 2)
    for angle in [-10, -5, -3, 3, 5, 10]:
        if w == 0 or h == 0:
            continue
        M = cv2.getRotationMatrix2D(center, angle, 1.0)
        rotated = cv2.warpAffine(region, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
        add(f"rotate_{angle}", rotated)
        add(f"rotate_{angle}_clahe", clahe_default.apply(rotated))

    try:
        dilated_bg = cv2.dilate(region, cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7)))
        bg_blur = cv2.medianBlur(dilated_bg, 21)
        diff = 255 - cv2.absdiff(region, bg_blur)
        shadow_removed = cv2.normalize(diff, None, 0, 255, cv2.NORM_MINMAX)
        add("shadow_removal", shadow_removed)
    except cv2.error:
        pass

    return methods


def _detect_microqr_in_region(
    region: np.ndarray,
    qr_index: int,
    detector: Any,
    *,
    verbose: bool = False,
) -> dict[str, Any] | None:
    if pb is None or detector is None:
        return None
    preprocessing_methods = _apply_preprocessing_methods(region)
    if not preprocessing_methods:
        return None
    if verbose:
        print(f"    Trying {len(preprocessing_methods)} preprocessing methods for QR {qr_index}...")
    best_result: dict[str, Any] | None = None
    attempts = 0
    for method_name, processed_region in preprocessing_methods:
        attempts += 1
        try:
            boof_region = pb.ndarray_to_boof(processed_region)
            detector.detect(boof_region)
        except Exception:
            continue
        if not detector.detections:
            continue
        qr = detector.detections[0]
        message = (qr.message or "").strip()
        candidate = {
            "message": message,
            "method": method_name,
            "qr_index": qr_index,
            "attempts": attempts,
        }
        if message:
            best_result = candidate
            break
        if best_result is None:
            best_result = candidate
    return best_result


def read_microqr_majority_vote(image: np.ndarray | str) -> str | None:
    if pb is None or cv2 is None:
        return None
    if isinstance(image, str):
        cv_img = cv2.imread(image, cv2.IMREAD_GRAYSCALE)
        if cv_img is None:
            return None
        try:
            boof_original = pb.load_single_band(image, np.uint8)
        except Exception:
            boof_original = pb.ndarray_to_boof(cv_img)
    else:
        cv_img = image
        boof_original = pb.ndarray_to_boof(image)

    preprocessing = [boof_original]
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    preprocessing.append(pb.ndarray_to_boof(clahe.apply(cv_img)))
    preprocessing.append(pb.ndarray_to_boof(cv2.equalizeHist(cv_img)))
    sharp_kernel = np.array([[-1, -1, -1], [-1, 9, -1], [-1, -1, -1]])
    preprocessing.append(pb.ndarray_to_boof(cv2.filter2D(cv_img, -1, sharp_kernel)))
    t1 = cv2.adaptiveThreshold(cv_img, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 11, 2)
    preprocessing.append(pb.ndarray_to_boof(t1))
    t2 = cv2.adaptiveThreshold(cv_img, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2)
    preprocessing.append(pb.ndarray_to_boof(t2))
    for alpha in [0.8, 1.0, 1.2, 1.5]:
        preprocessing.append(pb.ndarray_to_boof(cv2.convertScaleAbs(cv_img, alpha=alpha)))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    preprocessing.append(pb.ndarray_to_boof(cv2.morphologyEx(cv_img, cv2.MORPH_CLOSE, kernel)))
    preprocessing.append(pb.ndarray_to_boof(cv2.morphologyEx(cv_img, cv2.MORPH_OPEN, kernel)))

    messages: list[str] = []
    for prep in preprocessing:
        try:
            detector = pb.FactoryFiducial(np.uint8).microqr()
            detector.detect(prep)
        except Exception:
            continue
        for qr in detector.detections:
            msg = qr.message
            if msg:
                messages.append(msg)

    if not messages:
        return None
    counts = Counter(messages)
    message, _ = counts.most_common(1)[0]
    return message


def try_all_rotations(crop_array: np.ndarray, angles: range = range(-45, 46, 3)) -> list[tuple[np.ndarray, int]]:
    rotated_versions: list[tuple[np.ndarray, int]] = []
    for angle in angles:
        if angle == 0:
            rotated_versions.append((crop_array, angle))
        else:
            pil_img = Image.fromarray(crop_array)
            rotated = pil_img.rotate(angle, expand=True, fillcolor=255)
            rotated_versions.append((np.array(rotated), angle))
    return rotated_versions


def detect_in_crop(
    crop_array: np.ndarray,
    detector: Any,
    *,
    use_binary: bool = False,
    use_deblur: bool = False,
    use_morph: bool = False,
    use_clahe: bool = False,
    use_extreme_sharp: bool = False,
):
    if cv2 is None:
        raise RuntimeError("detect_in_crop requires OpenCV")
    processed = crop_array
    if use_clahe:
        processed = apply_clahe(processed)
    if use_extreme_sharp:
        processed = apply_extreme_sharpen(processed)
    if use_deblur:
        processed = apply_deblur_sharpen(processed)
    if use_morph:
        processed = apply_morphological_cleaning(processed)
    elif use_binary:
        _, processed = cv2.threshold(processed, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    temp_path = Path(tempfile.gettempdir()) / "polygon_qr_crop.png"
    cv2.imwrite(str(temp_path), processed)
    try:
        img_pb = pb.load_single_band(str(temp_path), np.uint8)
        detector.detect(img_pb)
        if detector.detections:
            return detector.detections[0]
        temp_path.unlink(missing_ok=True)
    except Exception:
        return None
    return None


def read_micro_qr_exhaustive(
    img_gray: np.ndarray,
    bbox: tuple[int, int, int, int],
    idx: int,
    detector: Any,
) -> str:
    if pb is None or cv2 is None or detector is None:
        return "Not found"
    x1, y1, x2, y2 = bbox
    print(f"  Reading QR code {idx} (bbox size: {max(1, x2 - x1)}x{max(1, y2 - y1)})...")
    regions = _generate_region_variants(img_gray, bbox, base_padding=40)
    if not regions:
        print("    ✗ Unable to extract region for QR decoding")
        return "Not found"
    best_partial: dict[str, Any] | None = None
    for region, padding in regions:
        quick = read_microqr_majority_vote(region)
        if quick:
            quick = quick.strip()
            print(f"    ✓ QR {idx}: '{quick}' via majority vote (padding: {padding})")
            return quick
        result = _detect_microqr_in_region(region, idx, detector, verbose=False)
        if result and result.get("message"):
            message = result["message"].strip()
            method = result.get("method", "unknown")
            attempts = result.get("attempts", "?")
            print(f"    ✓ QR {idx}: '{message}' (method: {method}, padding: {padding}, attempts: {attempts})")
            return message
        if result and best_partial is None:
            best_partial = result
    if best_partial:
        method = best_partial.get("method", "unknown")
        print(f"    ⚠ QR {idx}: shape detected but message empty (method: {method})")
    else:
        print(f"    ✗ QR {idx}: message not decoded after enhanced sampling")
    return "Not found"


__all__ = [
    "annotate_qr_messages",
    "apply_clahe",
    "apply_deblur_sharpen",
    "apply_extreme_sharpen",
    "apply_morphological_cleaning",
    "detect_in_crop",
    "extract_and_enhance_crop",
    "read_micro_qr_exhaustive",
    "read_microqr_majority_vote",
    "try_all_rotations",
    "prepare_table_qr_data",
]


def prepare_table_qr_data(
    frame: np.ndarray,
    detections: Sequence[Mapping[str, Any]],
    *,
    detector: Any | None = None,
    source: str = "left_yolo",
    log_errors: bool = True,
) -> None:
    """Extract table QR markers from detections, attempt to read their messages, and calibrate px->mm.

    Left/Right YOLO: class 5 = table micro qr
    """
    if frame is None or not len(detections):
        return

    img_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    height, width = frame.shape[:2]

    qr_detections = [det for det in detections if _is_table_qr_detection(det, source=source)]
    if cv2 is not None:
        annotate_qr_messages(img_gray, qr_detections, detector=detector)
    if not qr_detections:
        return
    try:
        calibrate_px_to_mm_from_qr_markers(qr_detections, source=source)
    except ValueError as exc:
        if log_errors:
            print(f"[Calib] Skipping calibration: {exc}")
