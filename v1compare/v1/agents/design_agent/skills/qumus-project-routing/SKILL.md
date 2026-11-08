---
name: qumus-project-routing
description: Route Qumus nanodevice contacts to imported pads using shared project rules for alignment markers, pad reuse, layer grouping, clearances, visual style, and verification gates.
---

# Qumus Project Routing

Use this skill when a Qumus/design_agent task needs project-specific metal lead routing, contact-to-pad fan-out, pad reuse, layer grouping, or verification. This skill supplements KlayoutClaw's generic `nanodevice_routing` behavior with maintained Qumus project conventions and the active `approved_device_guidance`.

## Device-Family Skills

This shared routing skill intentionally does not own device-specific external templates, pre-extracted profiles, stack parameters, terminal counts, gate dimensions, or contact-stack defaults. Load exactly one matching device-family skill when the request or `approved_device_guidance` identifies a known family:

- Graphene quantum Hall, graphene Hall bar, 4-pair Hall-bar, or edge-contact graphene device: `agents/design_agent/skills/graphene-qh-device/SKILL.md`
- ReSe2 transistor, 2D dual-gate transistor, split/local bottom-gate transistor, or bottom-contact geometry device: `agents/design_agent/skills/2d-dual-gate-transistor/SKILL.md`

For routing, apply the mandatory rules below plus `approved_device_guidance`. Explicit human or project-manager guidance overrides skill defaults. If neither device-family skill matches, keep this routing skill active but synthesize the device geometry from `approved_device_guidance`. Do not refresh rules from a reference layout unless the human explicitly supplies one for that run.

## Mandatory Project Rules

Apply these rules to every project design, except where a device-family skill explicitly replaces them:

- Reuse imported bond-pad regions declared by the active device-family skill whenever they are available.
- If no imported pad/template is provided for the active device, generate fallback bond pads as `250 um x 250 um` squares unless the human explicitly specifies another pad size.
- Treat the imported alignment-marker layer declared by the active device-family skill as required unless an explicit layer remap is approved.
- Use unfilled or transparent/outline display styles for layout content; do not use filled-color geometry for review output.
- Draw only actual physical/electrical circuit geometry in GDS: imported alignment markers and pads, material flakes, contact metal, gate metal, routing metal, and explicitly required real keep-outs. Do not draw layer-map boxes, review frames, substrate/chip frames, scale bars, inset frames, legend swatches, explanatory labels, annotation-only layers, or process-session placeholder layers unless the human explicitly requests documentation geometry. Put layer meanings, clean-room order, checks, and palette information in result JSON/run notes.
- Display colors must follow the project review palette: imported align markers use saturated pink/magenta; contact metal uses distinct orange; imported bond pads and outer routing metal use matching light blue; other device layers use a soft scientific-plot palette inspired by blue, amber, orange, pale teal, and green series.
- Use the metal layer grouping declared by the active device-family skill. If no device-family skill applies, derive contact, gate, routing, and handoff layers from `approved_device_guidance` instead of borrowing graphene or ReSe2 layer defaults.
- Different electrical wires/nets must not overlap or touch unless they are intentionally connected. Keep the minimum clearance between unrelated wires; use the clearance from `approved_device_guidance`, the active device-family skill, or the process rule deck when one is provided. If no numeric route-to-route clearance is provided, use `1.0 um` as the provisional minimum for fan-out/routing wires and record it in result metadata. Keep explicitly specified `500 nm` blank lateral gaps for the ReSe2 core bottom-metal strip array.
- Feature non-overlap rule: never let functionally distinct geometry unintentionally overlap, touch, coincide, or come closer than the minimum clearance. Newly drawn electrodes, gates, contacts, and routing metal must not overlap or intrude into the imported alignment-marker geometry or the imported bond-pad regions outside their intended landing necks. Overlap that is part of the intended stack (a layer covering the material below it, a designed contact/gate overlap, or an intentional handoff stitch) is expected and not a violation; only unintended collisions are errors. Before saving or verifying, run an explicit overlap check (a `pya.Region(...)` boolean AND, or equivalent) between new device geometry and the imported alignment-marker/bond-pad regions, and between unrelated electrical nets; also run clearance checks by sizing unrelated-net regions by half the required clearance and rejecting any intersection.
- Only the short outer-routing landing/neck segment that directly touches or overlaps a bond pad must be `50 um` wide unless the active device-family skill replaces this with local-template endpoint rules. Other outer-routing segments are not globally constrained to `50 um`; size them by net width, clearance, congestion, and smooth fan-out needs.
- Before saving or verifying, merge same-layer polygonal geometry using `pya.Region(...).merged()` or an equivalent KLayout operation. This is required to avoid slivers, internal seams, and notch-like Boolean artifacts.
- When any route/contact/gate must intentionally connect to pre-drawn geometry, bottom contacts, pads, or another metal layer, make the contact area at least `1.5 um x 1.5 um` unless explicit process guidance requires a larger overlap or the device-family skill provides a different approved value. Record each intended connection in result metadata.
- When the active device-family skill or `approved_device_guidance` requires cross-layer electrical handoffs, make every stitch overlap explicit, merge same-layer polygonal geometry before verification, and record the intended layer-to-layer connection in result metadata.
- Imported-pad landing necks must extend deeper into the pad than the shallow visible tab for non-graphene-template-backed routed designs: use at least `50 um` depth or twice the previous shallow depth, whichever is larger. The pad-touching neck endpoint must be a complete orthogonal rectangle/polygon with no missing corner, notch, chamfer, or diagonal bite.
- Clean-room process sessions are metadata on existing real geometry layers. Do not create standalone CR/session/process mask layers such as `903/90` or `904/90` unless the human explicitly requests fabrication mask deliverables. Effective device layer counts must exclude process-order-only metadata.
- Keep annotation, legend, inset, scale-bar, and review-only geometry out of GDS outputs unless explicitly requested, and always out of routing-rule extraction and verification.
- Before capturing any review screenshot, make the review/top design cell active and expand hierarchy. If the layout uses child-cell instances for the core device, insets, or repeated geometry, either flatten/copy their real geometry into the screenshot review cell or set `_layout_view.active_cellview().cell = review_cell` and call `_layout_view.max_hier()` before the KlayoutClaw `screenshot` call. A screenshot showing only child-cell names, instance boxes, or labels is invalid even if recursive geometry checks pass.
- Keep SDK/KLayout execution bounded: prefer one monolithic KLayout build script plus at most one verification/refinement pass, do not rely on state persisting across `execute_script` calls, and stop immediately after the required GDS, screenshot, and result JSON exist and checks pass.

## Implementation Guidance

Use KlayoutClaw MCP tools when KLayout is available:

- Create pin markers on temporary layers for contact tips and pad centers when using `auto_route`.
- Run `auto_route(dry_run=true)` first for dense or ambiguous fan-out; inspect `pairs[]` before committing routes.
- Use `pin_pairs_override` when the approved device net order/topology differs from ordered-loop assignment.
- Use `per_pair_obstacle_layers` when a route must avoid a material/gate/flake that other routes may cross.
- Call `route_inspect` with explicit `route_layer`, `contact_layers`, and `pad_layer`; do not rely on defaults.
- Prefer `evaluate_design` when available with a valid `python_path`; otherwise run equivalent `pya.Region` checks and record that `evaluate_design` was not run.

When auto-routing is not appropriate, draw explicit routes directly with `pya.Path` or simple boxes, but still satisfy the same gates: all required contacts reach pads, no unintended shorts, no mid-body crossings, and endpoints map to the intended contact/pad shapes.

## Conflict Handling

Treat `approved_device_guidance` and explicit human edits as higher priority than generic KlayoutClaw examples.

Reuse the imported pad regions declared by the active device-family skill when available. Generate fallback `250 um x 250 um` bond pads only when no imported pad/template source is available or the human explicitly asks for newly generated pads. If a device-family skill declares a fixed peripheral template, that template is the higher-priority peripheral source.

Flag these as open issues instead of silently choosing:

- Approved guidance omits a required net mapping or pad assignment and ordered-loop assignment would be ambiguous.
- Approved guidance asks a route to cross a material that would short the current device.
- Approved guidance gives top-gate/contact overlap rules that conflict with the active device's dielectric stack.
- Approved guidance has placeholder lithography rules without process approval.

## Result Requirements

Every routed design result should report:

- The routing skill version/path used.
- Layer map used for contacts, routes, pads, obstacles, and temporary pins.
- Effective device layer count, with no drawn review-only/process-order-only layers unless explicitly requested.
- Routing method: `auto_route`, manual `pya.Path`, or hybrid.
- Verification evidence: `route_inspect`, `evaluate_design`, or explicit `pya.Region` checks.
- Confirmation that legend/review-only geometry was not drawn in GDS unless explicitly requested and was excluded from routing and verification checks.
- Confirmation that the feature non-overlap check ran and found no unintended overlap between new device geometry and the imported alignment-marker/bond-pad regions, and no unintended same-layer overlap between different nets.
- Any deviations from approved guidance or these shared routing rules and why they were necessary.
- `status: ok`, `autonomous_continuation: true`, `human_review_required: false`, `review_gate: null`, and `human_review_gates: []` for all successful unblocked Qumus design outputs. If required net mapping, pad assignment, process approval, or geometry constraints remain unresolved, use `status: blocked`, list the blocking `open_issues`, and set `autonomous_continuation: false`; do not create a human review gate.
