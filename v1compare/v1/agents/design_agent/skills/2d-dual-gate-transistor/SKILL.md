---
name: 2d-dual-gate-transistor
description: Design Qumus ReSe2 / 2D dual-gate transistor devices with bottom-contact geometry, split local bottom gates, source-drain fingers, hBN encapsulation, and top graphite gate. Use for ReSe2 transistor, 2D dual gate transistor, split-gate transistor, or bottom-contact geometry requests.
---

# 2D Dual Gate Transistor

Use this skill when the human request or `approved_device_guidance` describes a ReSe2 transistor, 2D dual-gate transistor, local/split bottom-gate transistor, or bottom-contact geometry device. Explicit human or project-manager values override every default below.

Also use the shared project routing skill at `agents/design_agent/skills/qumus-project-routing/SKILL.md` for KLayout execution, imported alignment markers, pad reuse, layer grouping, visual style, geometry merging, non-overlap checks, and result metadata.

## Device References

Read [references/rese2-bottom-contact-profile.json](references/rese2-bottom-contact-profile.json) before generating an initial ReSe2 / 2D dual-gate bottom-contact layout. It captures the current canonical layer map, alignment-marker/pad source, material bboxes, bottom-gate/source-drain dimensions, routing zones, and verification checks.

- Required alignment-marker reference: `agents/design_agent/output/alignmarker2.GDS`
- Imported bond-pad source: `agents/design_agent/output/alignmarker2.GDS`
- Imported marker/pad layer: `1/0`
- Imported bond-pad policy: reuse the 21 imported pad-like landing regions from `alignmarker2.GDS`; these are imported geometry, not generated fallback squares.
- Current `alignmarker2.GDS` read policy: read layer `1/0` recursively from every top cell; do not assume the template has a single top cell.

## Complete Request Defaults

A short request such as "a ReSe2 transistor using bottom contact geometry" is complete. Do not ask the human to restate the stack, hBN thicknesses, gate widths, source/drain finger widths, or default contact geometry unless the request conflicts with these defaults.

- Device family: ReSe2 / 2D dual-gate bottom-contact transistor.
- Physical stack, top to bottom: top graphite gate / top hBN / ReSe2 / source-drain bottom contacts / bottom hBN / bottom metal contact-gate array.
- Active material: ReSe2 unless the approved guidance supplies a different 2D semiconductor.
- Top gate material: graphite.
- Bottom gate material: metal local/split gates.
- Contact style: bottom source-drain metal fingers under/at the active ReSe2 channel.
- Top hBN: `10-30 nm`, covering the active ReSe2 region and dielectric area under the top graphite gate. In inventory, accept hBN thickness bins `10-20` and `20-30` for this role.
- Bottom hBN: `5-20 nm`, separating the ReSe2 layer from the bottom local gate/contact-gate array according to the approved stack.
- Graphite top gate: database category `type_label='Gr'`, `thickness_label='manylayer'`. Do not require, infer, or filter on a graphite thickness in nm.

## Default Material Geometry

For ideal initial designs, draw virtual/synthetic flakes as quadrilateral-based irregular polygons with jagged or perturbed edges. Do not use ellipses, noisy circles, simple rectangles, or regular polygons for material flakes. Keep metal features straight-edged.

- Graphite top gate: approximately `36 um x 24 um`, covering the full ReSe2 flake and source/drain contact region.
- Top hBN: approximately `42 um x 30 um`, covering the ReSe2 and graphite-gate active region.
- ReSe2: approximately `16 um x 20 um`, overlapping the source/drain candidate region.
- Bottom hBN: approximately `48 um x 44 um`, covering the ReSe2 and source/drain contact region.
- Graphite contact landing: vertical `3 um x 15 um` metal pickup region near the left side of the graphite gate.

## Top Graphite Gate Pickup

- Place the graphite contact landing left of the ReSe2/channel outline.
- The horizontal gap from the landing's right edge to the leftmost ReSe2/channel boundary should be `15-20 um`.
- The gap must never be less than `15 um` unless the human explicitly overrides it.
- The graphite landing must overlap graphite sufficiently for direct electrical pickup.
- The graphite landing and its left metal pickup contact must remain exposed: top hBN must include a local opening/notch or avoid covering the landing/contact-overlap area.
- There must be no top hBN geometry between the graphite landing and the metal pickup.

## Bottom Gate and Source-Drain Array

Default central array, left to right:

- Four local bottom gates with widths `400 nm`, `400 nm`, `700 nm`, and `700 nm`.
- Adjacent bottom-gate edge-to-edge spacing: `1.5 um`.
- Source/drain finger width: `500 nm`.
- Source/drain channel gap: `500 nm`.
- Blank lateral gap between every neighboring metal strip: `500 nm`.
- The blank gap applies to bottom-gate-to-bottom-gate, bottom-gate-to-source/drain, and source/drain-to-source/drain spacing.
- Place one opposing source/drain pair in each inter-gate gap.
- Place one additional outer-edge source/drain pair beyond each end gate.
- For the canonical four-gate array, use five source/drain pairs total.
- The `1.5 um` bottom-gate spacing comes from `500 nm` clearance + `500 nm` source/drain finger width + `500 nm` clearance.

## Active-Region Layout

- Keep bottom gates and source/drain contacts straight, parallel, and evenly ordered through the central active region.
- Source/drain and bottom-gate metal may fan out only outside the active core.
- Use smooth radial fan-out outside the active core; do not bend inside the channel unless approved guidance requires it.
- The straight central bottom-gate and source/drain finger segments must extend past the ReSe2 target outline in the vertical direction.
- Default vertical overhang beyond the ReSe2 bbox: at least `2 um` on both top and bottom.
- Do not apply graphene Hall-bar or graphene edge-contact template rules to this device.

## Routing and Pads

Use the ReSe2 device reference above plus the shared project routing skill:

- Import or preserve `agents/design_agent/output/alignmarker2.GDS` when it is available.
- Reuse the 21 imported nominal-250 um pad-like landing regions from `alignmarker2.GDS` as routed bond pads when available.
- If no imported pad/template source is provided, generate fallback `250 um x 250 um` square bond pads unless the human explicitly specifies another pad design.
- Use the ReSe2 metal layer grouping. `Inner` is a fixed layout-coordinate routing zone independent of the active/core zone, with corners `(-30, -50)`, `(100, -50)`, `(-30, 60)`, and `(100, 60)` um.
- Keep source/drain fingers, graphite pickup, and contact-net fan-out in this inner zone on one inner-contact layer.
- Keep bottom-gate metal and bottom-gate-net fan-out in this inner zone on one separate inner-bottom-gate layer.
- Transition contact and bottom-gate nets to the shared outer-contact layer only after crossing outside this fixed rectangle.
- Use the shared outer-contact layer for large peripheral outer contacts and bond-pad lead segments beyond the fixed inner-zone boundary.
- For the outer part beyond the fixed inner-zone boundary, make electrode/routing metal wider than the central active-region fingers/gates using tapered or stepped widening. Default outer-route width should be at least `2 um`, and wider when pad approach/congestion allows; this widening must never reduce the active 500 nm blank gaps, create same-layer different-net contact, or violate the shared minimum clearance rule.
- Outer routing must not enter, extend through, run collinear with, share a long edge with, or merge into the straight central bottom-gate/source-drain section. The central active section is only the fixed gate/contact strip geometry; outer routes may connect to it only through short, explicit handoff/stitch geometry after the route has crossed outside the fixed inner-zone rectangle.
- Create intentional handoff stitches of at least `1.5 um x 1.5 um` between inner-contact or inner-bottom-gate metal and shared outer-contact metal; a cross-shaped stitch is allowed when space is tight.

## Real-Flake Adaptation

- Use real ReSe2, top hBN, bottom hBN, and graphite masks when provided by `candidate_mask_paths_by_role`.
- Preserve the approved local bottom-gate/source-drain pitch and ordering when adapting to real masks.
- If real ReSe2 does not support the central array plus required `2 um` vertical overhang, reject that placement or report the explicit conflict rather than silently shrinking critical spacings.
- Keep `rotation_deg=0` and `rotation_allowed=false` unless approved guidance explicitly permits rotation.

## Verification Checklist

Report these checks in `ideal_device_result.json`, `claude_design_result.json`, or run notes:

- Top graphite gate covers the active ReSe2/source-drain region.
- Top hBN isolates the top graphite gate from the ReSe2 channel while leaving the graphite pickup exposed.
- Bottom hBN covers the intended active/contact-gate area.
- Four bottom gates are present with `400/400/700/700 nm` widths unless overridden.
- Five source/drain pairs are present for the canonical four-gate design unless overridden.
- `500 nm` blank lateral gaps are maintained between neighboring metal strips.
- Bottom-gate spacing is `1.5 um` unless overridden.
- Source/drain and bottom-gate straight segments overhang ReSe2 vertically by at least `2 um`.
- The left graphite pickup landing is `3 um x 15 um`, placed `15-20 um` left of the ReSe2/channel outline, and not covered by top hBN.
- Outer electrode/routing segments beyond the fixed inner-zone boundary are wider than the central active-region fingers/gates, with tapered/stepped transitions and no same-layer different-net contact or clearance violation.
- No outer route is merged into, collinear with, or long-edge-adjacent to the straight central bottom-gate/source-drain section; only the explicit fixed-boundary handoff/stitch is allowed to connect inner and outer metal.
- No same-layer different-net metal shorts or unintended overlaps with imported markers/pads.
- All same-layer local metal is merged before save/verification.
- Result states `status: ok`, `autonomous_continuation: true`, `human_review_required: false`, `review_gate: null`, and `human_review_gates: []` when required artifacts exist and no blocking geometry, net-mapping, pad/template, or process issue remains unresolved.
