---
name: high-quality-flake-selection
description: Evaluate real Qumus 100X and 20X flake optical images, apply an hBN-only optical-colour sanity check, use 20X segmentation only as a geometry reference, rank candidates by device-specific shape and visible quality, and choose the complete physical-flake set required by an approved device stack before real-flake layout.
---

# High-quality flake selection

Use only candidates supplied in `candidate_payload.json`. Never introduce another mask, infer a missing identity, substitute a mask for either optical image, substitute one magnification for the other, or reuse one physical flake for two roles or two required instances.

Read the approved device guidance first. Treat its thickness, minimum size, coverage, contact, gate, channel, encapsulation, landing, and fabrication requirements as the selection target. A candidate is not good merely because it has the largest total area: the uninterrupted usable region must support its assigned device role at zero rotation.

For every candidate that could be selected, use the Claude Code `Read` tool to inspect both required optical sources and the segmentation reference when it exists:

- The real 100X optical image is the primary evidence for local surface quality and thickness uniformity. Look for residue, particles, folds, wrinkles, bubbles, cracks, scratches, thickness steps, focus or illumination problems, and whether the intended device region is visibly clean.
- The real 20X optical image is the primary evidence for the complete flake outline, large-scale continuity, usable placement region, edge truncation, and whether the role-specific device footprint can fit. Use it together with the calibrated pixel scale for geometry judgments.
- The segmented 20X mask, when supplied, is optional reference evidence only. Use it to locate the candidate, trace an approximate boundary, and make reproducible size/area estimates. Its absence does not block optical selection; Qumus must segment or relink an approved flake before real-flake layout. A mask is not authoritative for physical defects. A black gap, island, hole, split, or notch in it must never be called a physical hole, crack, tear, or disconnected region unless the corresponding feature is also visible in a real optical image. When optical and segmentation evidence disagree, follow the optical evidence, record the disagreement as a segmentation artifact or uncertainty, and do not penalize the flake as physically damaged.

A candidate without readable, existing `image_100x_path` and `image_20x_path` optical images is not selectable for a high-quality device. Do not use `preferred_image_path` or a segmentation mask as a substitute. Record `image_read_confirmed=true` and the exact `image_100x_path` as `real_image_path`, plus `image_20x_read_confirmed=true` and the exact `image_20x_path` as `real_image_20x_path`, in every selected flake's `quality_assessment`; these fields assert that both optical images were actually opened and judged, not merely that paths existed. Candidates with missing or unreadable optical evidence may remain in the ranked audit list as rejected evidence.

Do not claim defects that the optical images cannot resolve. Lower `evidence_confidence` and state the uncertainty instead. Treat thickness contrast as thickness variation or a step, not a hole, unless optical evidence shows a genuinely missing or torn region. Distinguish a segmentation artifact from a likely physical defect whenever possible.

For **hBN candidates only**, perform a second optical-colour sanity check with the vision capability used to read the real 100X and 20X images. This is independent of the segmentation class and is intended to catch incorrect hBN thickness segmentation:

- Compare the hBN body with adjacent bare substrate/background in the same frame; use relative optical colour, not absolute RGB values. Use the 100X image for local surface and edge evidence and the 20X image for whole-flake colour context.
- Useful hBN normally appears blue or cyan when relatively thin, or green to yellow when relatively thick. These are usable colour families subject to the normal surface-quality, continuity, geometry, and device-specific thickness checks; colour alone does not prove exact nanometre thickness.
- A purple or purple-magenta hBN body that is close to the surrounding background **and** has an optically visible thick edge/rim is clearly too thick for any hBN device role in this Qumus workflow. Hard-reject it even if segmentation, metadata, area, or shape says it is otherwise suitable. Record `reason="hBN optical colour and thick-edge check: too thick"`.
- If the whole image, including the bare substrate, has the same colour cast or illumination prevents a reliable relative comparison, do not infer thickness from colour. Mark the check uncertain, lower `evidence_confidence`, and require a better optical capture if this uncertainty prevents a fabrication-quality choice.
- Never apply this hBN colour rule to graphene, graphite, transition-metal dichalcogenides, or any other material. Their optical colours require their own material-specific evidence and must not be accepted or rejected using the hBN colour families above.

For every selected hBN flake, include an `hbn_color_judgement` object in `quality_assessment` with `relative_body_color`, `background_comparison`, `thick_edge_visible`, `classification` (`usable_color_family`, `too_thick`, or `uncertain`), and a concise `evidence` statement. Include the same classification and evidence in each hBN ranked audit entry so a human can verify whether colour affected the score or caused hard rejection.

Apply role-aware priorities:

- Active channels: prioritize a clean, continuous, thickness-uniform device region with sufficient contact/channel margins and no crack or fold crossing the transport path. For a graphene quantum Hall device, treat a `10 µm × 20 µm` Hall channel as preferred, but treat a continuous usable region that can support a `5 µm × 10 µm` Hall channel as the hard geometry minimum. A candidate that cannot fit `10 µm × 20 µm` must not be rejected on that fact alone: evaluate whether a compact channel between the preferred and minimum sizes can fit at zero rotation. The `3.0 µm` gate-metal-to-graphene-contact-metal clearance is a downstream metal-layout constraint, not extra graphene area that must surround the channel inside the flake.
- hBN dielectrics or encapsulants: prioritize continuous coverage, edge margin, absence of holes/folds/bubbles over raw area, and compatibility with all layers they must cover. For a graphene quantum Hall device, treat approximately `1200 µm²` as the soft preferred hBN area once the required coverage and margins are satisfied. Do not award a higher area score merely because an hBN flake is larger than this target, and do not make `1200 µm²` a hard minimum or maximum; a different area may win when its continuous usable region, cleanliness, or actual layout coverage is better.
- Graphite gates: prioritize continuous gate coverage plus a usable contact landing; reject narrow or broken current paths.
- Other materials: derive the functional region and failure modes from approved guidance.

Use hard rejection for unreadable/missing real 100X or 20X optical images, optically confirmed disconnected or truncated useful regions, failure of an explicit minimum geometry, optically confirmed contamination or damage in the intended device region, an hBN candidate that fails the hBN-only purple-body-plus-thick-edge check above, duplicate physical-flake use, or insufficient evidence to claim a fabrication-quality choice. Never reject optical selection merely because a 20X segmentation mask is missing, and never hard-reject from a mask-only apparent defect. If candidates exist but their optical paths are missing or unreadable, return a data-repair blocker with `action="relink_or_capture_optical_images"`; this condition must never request exfoliation. If both optical images were assessed and there are fewer fabrication-quality passing candidates than `required_flake_counts_by_role` requires, return `status="blocked"` with `action="exfoliate_and_search_one_chip"` for the deficient role.

Rank passing candidates using the following default rubric, which may be edited here for future runs:

- geometry fitness and role-specific usable margin: 20%
- visible cleanliness and defect risk: 30%
- continuity and thickness/optical uniformity: 35%
- confidence and agreement between both optical images and available segmentation/metadata: 10%; a missing optional mask is not itself a quality penalty, and optical evidence wins when segmentation disagrees
- inventory area/thickness priority as a tie-breaker: 5%; for graphene quantum Hall hBN, use closeness to the `1200 µm²` soft target instead of larger-is-better area ranking

Give each category a 0–100 score and provide `overall_score`, `evidence_confidence`, `image_read_confirmed`, `real_image_path`, `image_20x_read_confirmed`, `real_image_20x_path`, `segmentation_disagreements`, `observed_strengths`, `observed_risks`, and a concise `selection_reason`. Prefer the highest-scoring feasible combination, but explicitly justify any choice that differs from numerical order.

Choose the complete set required for one high-quality device. Use `required_flake_counts_by_role` as the exact cardinality contract. `selected_flakes_by_role` must map every required role to a list, even when its count is one, and `selected_flake_set` must flatten all selected records in physical stack-role order. Do not choose only one representative flake for the whole output.

Operator selection pins are binding. When `operator_selection_pins_by_role` or a role's `candidate_prefilters_by_role.required_flake_ids` is present, select the specified physical flake for that role and do not substitute another candidate. Still inspect both optical images and report `status="blocked"` if the pinned flake is unusable or lacks required optical evidence.

If `flake_fit_failure_report` is supplied, treat it as hard placement evidence from the design stage. Re-rank the remaining real candidates against the reported failed geometry and replacement requirements, and do not return the same impossible combination. A design report may trigger a new selection only when it states `layout_impossible=true` and documents exhausted bounded placement checks; aesthetic preferences or a merely suboptimal score are not sufficient.

Human gate rejection is binding for the current device attempt. When `excluded_flake_identities` or `human_rejection_history` is present, never select any rejected physical flake again. A selected-flake rejection replaces only the marked physical flakes while recomputing compatibility of the complete set. A whole-combination rejection excludes every physical flake in that proposed set and selects a new complete set from the remaining inventory. If the exclusions leave too few feasible candidates for any required role, return `status="blocked"` with the normal role-specific `retry_requests`; do not restore a rejected flake to avoid exfoliation. Human rejection/reselection is bounded by the finite supplied inventory because every rejection permanently excludes at least one candidate from this device attempt.

Write the final result to the exact output path from the prompt. Preserve every selected candidate's original database fields and add `quality_assessment` and `selection_reason`. Include ranked assessments for every supplied candidate for auditability. Do not perform layout design, mask rotation, stacking, hardware actions, or human approval yourself.
