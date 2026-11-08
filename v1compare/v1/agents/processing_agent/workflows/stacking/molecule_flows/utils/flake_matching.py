from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np

from .tools import convert_pt_unit

UNSHARP_AMT = 1.0
UNSHARP_SIGMA = 2.0
CLAHE_CLIP = 2.0
CLAHE_GRID = (8, 8)
HIGH_PASS_SIGMA = 12


def enhance_keep_color(bgr):
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    l_chan, a_chan, b_chan = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=CLAHE_CLIP, tileGridSize=CLAHE_GRID)
    l_clahe = clahe.apply(l_chan)
    blur = cv2.GaussianBlur(l_clahe, (0, 0), UNSHARP_SIGMA)
    l_sharp = cv2.addWeighted(l_clahe, 1 + UNSHARP_AMT, blur, -UNSHARP_AMT, 0)
    if HIGH_PASS_SIGMA > 0:
        low = cv2.GaussianBlur(l_sharp, (0, 0), HIGH_PASS_SIGMA)
        l_out = cv2.addWeighted(l_sharp, 1.0, low, -1.0, 128)
        l_out = np.clip(l_out, 0, 255).astype(np.uint8)
    else:
        l_out = l_sharp
    out = cv2.cvtColor(cv2.merge([l_out, a_chan, b_chan]), cv2.COLOR_LAB2BGR)
    return out, l_out


def rotate_image_gray(gray_u8, angle_deg):
    h, w = gray_u8.shape[:2]
    mat = cv2.getRotationMatrix2D((w / 2, h / 2), angle_deg, 1.0)
    return cv2.warpAffine(
        gray_u8,
        mat,
        (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REFLECT,
    )


def search_flake(label, img_path, tpl_path, out_path, scale=0.05, rot_angles=None):
    rot_angles = [0] if rot_angles is None else rot_angles
    img_bgr = cv2.imread(img_path, cv2.IMREAD_COLOR)
    tpl_bgr = cv2.imread(tpl_path, cv2.IMREAD_COLOR)
    if img_bgr is None:
        raise RuntimeError("img reading failed")
    if tpl_bgr is None:
        raise RuntimeError("template reading failed")

    img_w, img_h = img_bgr.shape[1], img_bgr.shape[0]
    img_bgr_enh, img_l = enhance_keep_color(img_bgr)
    _, tpl_l = enhance_keep_color(tpl_bgr)
    best = dict(score=-1.0, loc=None, wh=None, ang=None, res=None)
    h_img, w_img = img_l.shape[:2]

    for ang in rot_angles:
        tpl_rot = rotate_image_gray(tpl_l, ang)
        th = max(8, int(round(tpl_rot.shape[0] * scale)))
        tw = max(8, int(round(tpl_rot.shape[1] * scale)))
        if th >= h_img or tw >= w_img:
            continue
        tpl_s = cv2.resize(tpl_rot, (tw, th), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(
            img_l.astype(np.float32),
            tpl_s.astype(np.float32),
            cv2.TM_CCOEFF_NORMED,
        )
        _, maxv, _, maxloc = cv2.minMaxLoc(res)
        if maxv > best["score"]:
            best.update(score=float(maxv), loc=maxloc, wh=(tw, th), ang=float(ang), res=res)

    x, y = best["loc"]
    tw, th = best["wh"]
    vis = img_bgr_enh.copy()
    cv2.rectangle(vis, (x, y), (x + tw, y + th), (0, 255, 0), 2)
    cv2.circle(vis, (int(img_w / 2), int(img_h / 2)), int(img_h * 2600 / 4480), (0, 0, 255), 2)
    cv2.imwrite(str(out_path), vis)
    best["loc"] = convert_pt_unit(best["loc"], view="5x")
    return best


def flake_detection(img, view="50x", id="ori", coefficient=None, debug_dir="./test"):
    coefficient = [20, 30] if coefficient is None else coefficient
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, coefficient[0], coefficient[1])
    edges = cv2.dilate(edges, None, iterations=1)
    edges = cv2.bitwise_and(edges, edges)
    os.makedirs(debug_dir, exist_ok=True)
    cv2.imwrite(os.path.join(debug_dir, f"edges_{view}_{id}.png"), edges)

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask_flake = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)
    mask_flake = cv2.morphologyEx(mask_flake, cv2.MORPH_DILATE, kernel, iterations=1)

    contours, _ = cv2.findContours(mask_flake, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    mask_clean = np.zeros_like(mask_flake)
    max_w, max_h = img.shape[1], img.shape[0]
    if view == "5x":
        thresh = max_w * max_h * 0.0002
    elif view == "20x":
        thresh = max_w * max_h * 0.0008
    else:
        thresh = max_w * max_h * 0.0032

    for contour in contours:
        if cv2.contourArea(contour) > thresh:
            cv2.drawContours(mask_clean, [contour], -1, 255, -1)

    vis = img.copy()
    vis[mask_clean == 255] = [0, 255, 0]
    cv2.imwrite(os.path.join(debug_dir, f"detected_flake_{view}_{id}.png"), vis)
    cv2.imwrite(os.path.join(debug_dir, f"mask_clean_{view}_{id}.png"), mask_clean)
    return mask_clean


def largest_component(mask):
    num, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=4)
    if num <= 1:
        return mask
    idx = 1 + np.argmax(stats[1:, cv2.CC_STAT_AREA])
    out = np.zeros_like(mask)
    out[labels == idx] = 255
    return out


def mask_centroid(mask):
    moments = cv2.moments(mask, binaryImage=True)
    if abs(moments["m00"]) < 1e-9:
        return None
    return (moments["m10"] / moments["m00"], moments["m01"] / moments["m00"])


def locate_template_in_target(
    mask_template,
    mask_target,
    ratio_guess=0.2,
    scale_rel=0.3,
    n_scales=15,
    angle_range=15,
    n_angles=15,
    save_dir="./debug",
):
    os.makedirs(save_dir, exist_ok=True)
    tpl = largest_component(mask_template)
    tgt = mask_target.copy()
    best = {"score": -1}
    h_tgt, w_tgt = tgt.shape[:2]
    scales = np.linspace(ratio_guess * (1 - scale_rel), ratio_guess * (1 + scale_rel), n_scales)
    angles = np.linspace(-angle_range, angle_range, n_angles)

    for scale in scales:
        tpl_s = cv2.resize(
            tpl,
            (int(tpl.shape[1] * scale), int(tpl.shape[0] * scale)),
            interpolation=cv2.INTER_NEAREST,
        )
        h_tpl, w_tpl = tpl_s.shape[:2]
        if h_tpl >= h_tgt or w_tpl >= w_tgt:
            continue
        for ang in angles:
            mat = cv2.getRotationMatrix2D((w_tpl / 2, h_tpl / 2), ang, 1.0)
            tpl_r = cv2.warpAffine(
                tpl_s,
                mat,
                (w_tpl, h_tpl),
                flags=cv2.INTER_NEAREST,
                borderMode=cv2.BORDER_CONSTANT,
            )
            res = cv2.matchTemplate(tgt, tpl_r, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val > best["score"]:
                best = {
                    "score": float(max_val),
                    "scale": float(scale),
                    "angle": float(ang),
                    "top_left_target": (int(max_loc[0]), int(max_loc[1])),
                    "box_target": (int(max_loc[0]), int(max_loc[1]), int(w_tpl), int(h_tpl)),
                    "template_rot_scaled": tpl_r,
                }

    pt = None
    if best["score"] > 0:
        x, y, w_box, h_box = best["box_target"]
        centroid = mask_centroid(best["template_rot_scaled"])
        if centroid is None:
            cx = x + w_box / 2.0
            cy = y + h_box / 2.0
            best["center_source"] = "bbox_center_fallback"
        else:
            cx = x + float(centroid[0])
            cy = y + float(centroid[1])
            best["center_source"] = "mask_centroid"
        best["center_target"] = (cx, cy)
        overlay = cv2.cvtColor(tgt, cv2.COLOR_GRAY2BGR)
        cnts, _ = cv2.findContours(best["template_rot_scaled"], cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cnts_shifted = [c + np.array([[x, y]]) for c in cnts]
        cv2.drawContours(overlay, cnts_shifted, -1, (0, 255, 0), 2)
        cv2.circle(overlay, (int(round(cx)), int(round(cy))), 4, (0, 0, 255), -1)
        cv2.imwrite(os.path.join(save_dir, "best_match_on_target.png"), overlay)
        pt = (cx, cy)
    return best, pt
