from __future__ import annotations

import os
import time

import cv2
import numpy as np

STEP = -0.01
MAX_STEP = 10000
APPROACHING_STEP = -0.1
THRESH = 40
AREA_MIN = 5000
NEWTON_THRESH = 30
AREA_MIN_NEWTON = 5000
BLUR_KERNEL = 9


def load_single_step(step_path):
    img = cv2.imread(str(step_path), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"Cannot find image: {step_path}")
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    return gray, img


def detect_first_touching_point(camera, arm, data_path, out_path):
    step_idx = 0
    ref_step = os.path.join(data_path, f"step_{step_idx}.png")
    print(f"[PICKUP] Touch detection reference capture -> {ref_step}", flush=True)
    camera.wait_focus_and_take_photo(min_blur=5, path=".", save_path=ref_step)
    ref_gray, _ = load_single_step(ref_step)
    ref_uint8 = cv2.convertScaleAbs(ref_gray)

    while True:
        step_idx += 1
        step_dir = os.path.join(data_path, f"step_{step_idx}.png")
        print(f"[PICKUP] Touch detection step {step_idx}: jog z by {STEP}", flush=True)
        arm.jog_axis(axis="z", step=STEP)
        time.sleep(1)
        camera.wait_focus_and_take_photo(min_blur=4, path=".", save_path=step_dir)
        cur_gray, color_img = load_single_step(step_dir)
        cur_uint8 = cv2.convertScaleAbs(cur_gray)

        diff = cv2.absdiff(ref_uint8, cur_uint8)
        diff_norm = cv2.normalize(diff, None, 0, 255, cv2.NORM_MINMAX)
        _, mask = cv2.threshold(diff_norm, NEWTON_THRESH, 255, cv2.THRESH_BINARY)
        mask = mask.astype(np.uint8)

        num_labels_raw, labels_raw, stats_raw, centroids_raw = cv2.connectedComponentsWithStats(mask, connectivity=4)
        filtered_mask = np.zeros_like(mask)

        for i in range(1, num_labels_raw):
            w = int(stats_raw[i, cv2.CC_STAT_WIDTH])
            h = int(stats_raw[i, cv2.CC_STAT_HEIGHT])
            if h == 0 or w == 0:
                continue
            ar = w / h
            if stats_raw[i, cv2.CC_STAT_AREA] > AREA_MIN_NEWTON and 0.8 <= ar <= 1.25:
                filtered_mask[labels_raw == i] = 255

        if np.count_nonzero(filtered_mask) > 0:
            num_labels_f, labels_f, stats_f, cents_f = cv2.connectedComponentsWithStats(filtered_mask, connectivity=4)
            best_label = 1 + np.argmax(stats_f[1:, cv2.CC_STAT_AREA])
            x = int(stats_f[best_label, cv2.CC_STAT_LEFT])
            y = int(stats_f[best_label, cv2.CC_STAT_TOP])
            w = int(stats_f[best_label, cv2.CC_STAT_WIDTH])
            h = int(stats_f[best_label, cv2.CC_STAT_HEIGHT])
            cx_i, cy_i = map(int, np.round(cents_f[best_label]))
            cv2.rectangle(color_img, (x, y), (x + w, y + h), (0, 0, 255), 2)
            cv2.circle(color_img, (cx_i, cy_i), 4, (0, 255, 0), -1)
            cv2.putText(
                color_img,
                f"center=({cx_i},{cy_i})",
                (x, max(0, y - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (0, 0, 255),
                1,
            )
            cv2.imwrite(out_path, color_img)
            print(f"[PICKUP] First touching point found at ({cx_i}, {cy_i}) on step {step_idx}", flush=True)
            return (cx_i, cy_i), step_idx


def point_to_edge_distance_inside(point_xy, contour):
    px, py = float(point_xy[0]), float(point_xy[1])
    d = cv2.pointPolygonTest(contour, (px, py), True)
    return int(d) if d > 0 else 0


def stake_target_flake(
    camera,
    arm,
    target_pt,
    safe_dist,
    delta_z,
    data_path,
    out_path_base,
    *,
    on_motion_start=None,
    on_complete=None,
):
    os.makedirs(out_path_base, exist_ok=True)
    step_idx = 0
    ref_step = os.path.join(data_path, f"step_{step_idx}.png")
    print(f"[PICKUP] Stacking reference capture -> {ref_step}", flush=True)
    camera.wait_focus_and_take_photo(min_blur=5.5, path=".", save_path=ref_step)
    ref_gray, _ = load_single_step(ref_step)
    ref_uint8 = cv2.convertScaleAbs(ref_gray)
    if BLUR_KERNEL > 0:
        ref_uint8 = cv2.GaussianBlur(ref_uint8, (BLUR_KERNEL, BLUR_KERNEL), 0)

    # This callback is the durable boundary between a safely replayable setup
    # and an ambiguous physical Z move. It must run immediately before the
    # first transfer-arm motion.
    if on_motion_start is not None:
        on_motion_start()
    arm.set_kst_speed(axis="z", max_vel=0.005, accel=0.5)
    print(f"[PICKUP] Move to pre-approach height by {-delta_z + 0.1}", flush=True)
    arm.move_axis_by(axis="z", delta=-delta_z + 0.1)

    while True:
        if step_idx >= MAX_STEP:
            raise RuntimeError("Exceeded maximum steps without successful stacking. Please check the setup.")
        step_idx += 1
        step_dir = os.path.join(data_path, f"step_{step_idx}.png")
        arm.set_kst_speed(axis="z", max_vel=0.005, accel=0.5)
        print(f"[PICKUP] Stacking step {step_idx}: move z by {APPROACHING_STEP}", flush=True)
        arm.move_axis_by(axis="z", delta=APPROACHING_STEP)
        camera.wait_focus_and_take_photo(min_blur=5.5, path=".", save_path=step_dir)

        cur_gray, color_img = load_single_step(step_dir)
        cur_uint8 = cv2.convertScaleAbs(cur_gray)
        if BLUR_KERNEL > 0:
            cur_uint8 = cv2.GaussianBlur(cur_uint8, (BLUR_KERNEL, BLUR_KERNEL), 0)

        diff = cv2.absdiff(ref_uint8, cur_uint8)
        diff_norm = cv2.normalize(diff, None, 0, 255, cv2.NORM_MINMAX)
        _, mask = cv2.threshold(diff_norm, THRESH, 255, cv2.THRESH_BINARY)
        mask = mask.astype(np.uint8)

        num_labels_raw, labels_raw, stats_raw, _ = cv2.connectedComponentsWithStats(mask, connectivity=4)
        filtered_mask = np.zeros_like(mask)
        for i in range(1, num_labels_raw):
            w = int(stats_raw[i, cv2.CC_STAT_WIDTH])
            h = int(stats_raw[i, cv2.CC_STAT_HEIGHT])
            if h == 0 or w == 0:
                continue
            ar = w / h
            if stats_raw[i, cv2.CC_STAT_AREA] > AREA_MIN and 0.1 <= ar <= 10.0:
                filtered_mask[labels_raw == i] = 255

        mask_path = os.path.join(out_path_base, f"mask_step_{step_idx}.png")
        cv2.imwrite(mask_path, filtered_mask)
        if np.count_nonzero(filtered_mask) > 0:
            num_labels_f, labels_f, stats_f, _ = cv2.connectedComponentsWithStats(filtered_mask, connectivity=4)
            max_label = None
            if num_labels_f > 1:
                areas = stats_f[1:, cv2.CC_STAT_AREA]
                max_label = 1 + int(np.argmax(areas))
            if max_label is not None:
                largest_mask = (labels_f == max_label).astype(np.uint8) * 255
                contours, _ = cv2.findContours(largest_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                contour = max(contours, key=cv2.contourArea)
                vis = color_img.copy()
                cv2.drawContours(vis, [contour], -1, (0, 255, 0), 2)
                tp = (int(target_pt[0]), int(target_pt[1]))
                cv2.circle(vis, tp, 4, (0, 0, 255), -1)
                out_path = os.path.join(out_path_base, f"step_{step_idx}.png")
                cv2.imwrite(out_path, vis)
                dist_px = point_to_edge_distance_inside(target_pt, contour)
                print(
                    f"[PICKUP] Stacking step {step_idx}: safe distance check dist_px={dist_px}, threshold={safe_dist}",
                    flush=True,
                )
                if dist_px > safe_dist:
                    camera.autofocus_once()
                    print(f"[PICKUP] Target flake covered safely at step {step_idx}", flush=True)
                    if on_complete is not None:
                        on_complete(step_idx)
                    return contour, step_idx
