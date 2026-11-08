from __future__ import annotations

from typing import Optional, Tuple

# Approved (target_T_C, hold_V_V) pairs for exfoliation.
EXFOLIATION_SETPOINT_PAIRS: tuple[tuple[float, float], ...] = (
    (22.0, 0.0),
    (30.0, 6.5),
    (50.0, 10.0),
    (70.0, 13.5),
    (90.0, 17.0),
)

MIN_TAPE_PEEL_SPEED_V = 50.0
MAX_TAPE_PEEL_SPEED_V = 500.0
DEFAULT_TAPE_PEEL_SPEED_V = 500.0
MIN_DWELL_TIME_S = 10.0
MAX_DWELL_TIME_S = 90.0


def validate_exfoliation_setpoint_pair(
    target_T: float,
    hold_V: Optional[float],
) -> Tuple[float, float]:
    """Validate target/hold values against approved setpoint pairs.

    Returns normalized (target_T, hold_V). If hold_V is None, fills with the
    matched value for the chosen target setpoint.
    """
    t = float(target_T)

    # Match target to one allowed setpoint with tiny numeric tolerance.
    tol = 1e-6
    matched_pair = None
    for allowed_t, allowed_v in EXFOLIATION_SETPOINT_PAIRS:
        if abs(t - allowed_t) <= tol:
            matched_pair = (allowed_t, allowed_v)
            break

    if matched_pair is None:
        allowed_t_text = ", ".join(f"{tp:g}" for tp, _ in EXFOLIATION_SETPOINT_PAIRS)
        raise ValueError(
            f"target_T={t:.3f} is unsupported. Allowed target_T values: {allowed_t_text} (C)."
        )

    allowed_t, allowed_v = matched_pair
    if hold_V is None:
        return float(allowed_t), float(allowed_v)

    v = float(hold_V)
    if abs(v - allowed_v) > tol:
        allowed_pairs_text = ", ".join(f"({tp:g}, {vp:g})" for tp, vp in EXFOLIATION_SETPOINT_PAIRS)
        raise ValueError(
            f"Invalid setpoint pair: target_T={t:.3f}, hold_V={v:.3f}. "
            f"Allowed (target_T, hold_V) pairs are: {allowed_pairs_text}."
        )

    return float(allowed_t), float(v)


def validate_tape_peel_speed_v(tape_peel_speed_v: Optional[float]) -> float:
    """Validate exfoliator peel speed against the approved range."""
    v = float(tape_peel_speed_v) if tape_peel_speed_v is not None else DEFAULT_TAPE_PEEL_SPEED_V
    if not (MIN_TAPE_PEEL_SPEED_V <= v <= MAX_TAPE_PEEL_SPEED_V):
        raise ValueError(
            f"tape_peel_speed_v={v:.3f} is out of range. Allowed range: "
            f"{MIN_TAPE_PEEL_SPEED_V:g} to {MAX_TAPE_PEEL_SPEED_V:g}."
        )
    return v


def validate_dwell_time_s(dwell_time_s: Optional[float]) -> float:
    """Validate exfoliation dwell time against the approved range."""
    v = float(dwell_time_s) if dwell_time_s is not None else 10.0
    if not (MIN_DWELL_TIME_S <= v <= MAX_DWELL_TIME_S):
        raise ValueError(
            f"dwell_time_s={v:.3f} is out of range. Allowed range: "
            f"{MIN_DWELL_TIME_S:g} to {MAX_DWELL_TIME_S:g} seconds."
        )
    return v
