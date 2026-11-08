---
name: flake-selection
description: How to judge real Qumus 100X and 20X optical images of candidate flakes and choose the complete flake set for one device (ported from v1 high-quality-flake-selection). Judgement criteria only; the submit tool enforces the hard rules.
---

# Flake selection: how to judge

Ported from v1 `agents/flake_selection_agent/skills/high-quality-flake-selection/SKILL.md`.
The hard rules v1 wrote here (only supplied candidates, exact count per role, no
flake used twice, vetoed flakes never again, both optical images actually viewed,
required audit fields) are now checked in code by `submit_flake_selection`. If it
rejects a submission, read the reason and fix that point.

## Eight-contact device_design_v1

When `get_flake_candidates` returns `design_contract=device_design_v1`,
follow the Picker prompt's complete-outline selection route and propose rigid
source translations/rotations in `design_placements`. The legacy zero-rotation
and rectangular-channel defaults below do not apply. These are pose proposals,
not hardware commands or etched-mask design; Designer owns artifact acceptance.
All image, cleanliness, continuity and evidence requirements still apply.

## Target

Read the device plan from `get_flake_candidates` first. Its role requirements
(material, thickness, minimum size, coverage, contact, gate, channel,
encapsulation) are the selection target. A candidate is not good merely because
it has the largest total area: the uninterrupted usable region must support its
assigned role under the accepted design contract. Legacy rectangle plans require
that fit at zero rotation.

## Evidence

Open both optical images of every candidate you might select with `view_flake_images`.

- **100X optical image**: primary evidence for local surface quality and thickness
  uniformity. Look for residue, particles, folds, wrinkles, bubbles, cracks,
  scratches, thickness steps, focus or illumination problems, and whether the
  intended device region is visibly clean.
- **20X optical image**: primary evidence for the whole outline, large-scale
  continuity, usable placement region, edge truncation, and whether the role's
  device footprint fits. Use it with the pixel scale for geometry.
- **20X segmentation mask** (optional): only a reference to locate the flake and
  estimate size. It is not evidence of physical defects. A gap, island, hole or
  notch that is only in the mask must not be called a physical hole or crack.
  When optical images and the mask disagree, follow the optical images and
  record the disagreement as a segmentation artifact.

Do not claim defects the images cannot resolve; lower `evidence_confidence` and
say what is uncertain. Thickness contrast is a thickness step, not a hole,
unless the optical image shows a missing or torn region.

## hBN colour check (hBN only)

- Compare the hBN body with the bare substrate next to it in the same frame
  (relative colour, not absolute RGB). 100X for local surface and edges, 20X for
  whole-flake colour context.
- Useful hBN is usually blue or cyan (thinner) or green to yellow (thicker).
  Colour alone does not give nanometre thickness.
- A purple or purple-magenta body close to the background colour **and** a
  visible thick edge/rim means too thick for any hBN role in Qumus: classify
  `too_thick` and do not select it, whatever the metadata says.
- If the whole frame has a colour cast or the illumination prevents a relative
  comparison, classify `uncertain`, lower `evidence_confidence`, and ask for a
  better capture if that blocks a fabrication-quality choice.
- Never apply this rule to graphene, graphite, TMDs or other materials.

For each hBN flake report `hbn_color_judgement` with `relative_body_color`,
`background_comparison`, `thick_edge_visible`, `classification`
(`usable_color_family` | `too_thick` | `uncertain`) and a short `evidence`.

## Surface residue (every material)

A good flake has the right thickness, enough area and a clean surface: no
excessive tape residue on the chip around and on it (Jialei, 2026-10-21).
Judge it on the 100X image and report `surface_residue` for every candidate:

- `clean`: the substrate around the flake is clear; at most a few isolated specks
  away from the device region.
- `light`: some residue spots or a thin film near the flake, but the intended
  device region is clear.
- `heavy`: residue patches or film touching or covering the device region, or
  so much around the flake that the stamp will pick it up. Never select it.
- `uncertain`: the image cannot tell (focus, illumination); lower
  `evidence_confidence` and say what is missing.

Residue is not the same as a thickness step or a fold: residue sits on top,
often with an irregular, dirty-looking edge, and is also on the bare substrate.
Give one line of `residue_evidence`. The judgement is stored on the flake and
counts when the lab judges how well an exfoliation recipe worked.

## Legacy role geometry defaults

- **Active channel**: clean, continuous, thickness-uniform region with contact and
  channel margins; no crack or fold across the transport path. Graphene quantum
  Hall: `10 µm × 20 µm` Hall channel preferred, `5 µm × 10 µm` is the minimum. A
  flake that cannot fit `10 × 20` is not rejected for that alone; check whether a
  channel between the two sizes fits at zero rotation. The `3.0 µm` gate-metal to
  contact-metal clearance is a metal-layout rule, not extra graphene area.
- **hBN dielectric / encapsulant**: continuous coverage, edge margin, no holes,
  folds or bubbles, compatible with every layer it must cover. Graphene QH:
  about `1200 µm²` is a soft target, not a minimum or maximum; do not reward area
  beyond it.
- **Graphite gate**: continuous gate coverage plus a usable contact landing; reject
  narrow or broken current paths.
- **Other materials**: derive the functional region and failure modes from the
  device plan.

## Reject a candidate when

Optical images missing or unreadable; useful region optically disconnected or
truncated; an explicit minimum geometry fails; contamination or damage visible
in the device region; hBN fails the colour check above; or the evidence is not
enough to claim a fabrication-quality choice. Never reject because the optional
mask is missing, and never from a mask-only defect.

If candidates exist but have no readable optical images, report the role as
blocked with action `relink_or_capture_optical_images` (never exfoliation). If
both images were judged and too few candidates pass for a role, report it
blocked with action `exfoliate_and_search_one_chip`.

## Scoring (0-100 per category)

- geometry fitness and role-specific usable margin: 20%
- visible cleanliness and defect risk: 30%
- continuity and thickness/optical uniformity: 35%
- confidence and agreement between both optical images and segmentation: 10%
  (a missing optional mask is not a penalty)
- inventory tie-breaker: 5% (graphene QH hBN: closeness to `1200 µm²`, not
  larger-is-better)

Give `overall_score`, `evidence_confidence`, `observed_strengths`,
`observed_risks`, `segmentation_disagreements` and a short `selection_reason`.
Prefer the highest-scoring feasible combination and justify any choice that
differs from numerical order. Assess every supplied candidate, including the
ones you reject, so a human can audit the ranking.

If a design-fit failure report is supplied (the designer proved a combination
cannot be laid out), do not return the same combination.

Do not design device masks, stack, move hardware or ask for approval. Only for
`device_design_v1`, supply the proposed rigid source poses requested in
`design_placements`; Designer owns layout and artifact acceptance.
