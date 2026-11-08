---
name: graphene-qh-device
description: Design Qumus graphene quantum Hall / Hall-bar devices with edge contacts, hBN encapsulation, bottom graphite gate, and the project graphene electrode template. Use for graphene QH, graphene Hall bar, 4-pair Hall-bar, or edge-contact graphene requests.
---

# Graphene QH Device

Use this skill when the human request or `approved_device_guidance` describes a graphene quantum Hall device, graphene Hall bar, 4-pair Hall-bar geometry, or graphene device using edge contacts. Explicit human or project-manager values override every default below. In older guidance, an unqualified "approximately 10 um x 20 um" is the preferred size, not an explicit hard minimum; only a clearly stated stricter minimum overrides the `5 um x 10 um` fallback.

Also use the shared project routing skill at `agents/design_agent/skills/qumus-project-routing/SKILL.md` for KLayout execution, marker/pad conventions, visual style, geometry merging, non-overlap checks, and result metadata.

## Complete Request Defaults

A short request such as "graphene quantum Hall device using edge contact" is complete. Do not ask the human to restate the layer stack, hBN thicknesses, default terminal count, or contact metal unless the request conflicts with these defaults.

- Device family: edge-contact graphene quantum Hall / Hall-bar device.
- Physical stack, top to bottom: top hBN / monolayer graphene / bottom hBN / bottom graphite gate.
- Active material: monolayer graphene.
- Preferred active graphene Hall-channel size: approximately `10 um x 20 um`.
- Minimum active graphene Hall-channel size: `5 um x 10 um`. This is a usable channel-body minimum, not a preferred target; contact arms and edge-contact landings are evaluated separately against the real flake boundary.
- Default measurement geometry: standard Hall bar.
- Default terminal count: `10` graphene transport terminals plus `1` graphite gate terminal.
- Default graphene transport terminals: `2` source/drain current terminals at the channel ends, plus `4` pairs of side voltage probes along the channel between them.
- Top hBN: `10-30 nm`, covering the whole graphene area. In inventory, accept hBN thickness bins `10-20` and `20-30` for this role.
- Bottom hBN: `35-40 nm`, covering the whole graphene area and protecting the bottom graphite gate during edge-contact etching.
- Bottom graphite gate: database category `type_label='Gr'`, `thickness_label='manylayer'`, covering the entire patterned graphene Hall-bar footprint. It does not need to cover unused graphene outside the Hall-bar footprint. Do not require, infer, or filter on a graphite thickness in nm.
- Graphite gate access: expose the graphite on exactly one side of the vdW stack (the gate side) as a single tab/contact portion outside both the top and bottom hBN coverage for direct gate pickup. Do not expose graphite on more than one side.
- Gate contact shape: draw the graphite gate pickup as a T whose crossbar faces the stack (see `Bottom Graphite Gate - Single-Side Isolation and T-Contact`).
- Graphene contact style: edge contacts unless explicitly overridden.
- Default edge-contact metal stack: `5 nm Cr / 60-65 nm Au`.

## Geometry Rules

- Pattern graphene as one central Hall-bar body, not as disconnected islands.
- Make source and drain contacts span the full channel width at the two long-axis ends.
- Place the 4 voltage-probe pairs symmetrically along the Hall-bar channel when the user does not specify a different probe layout.
- Keep the Hall-bar channel inside the hBN-covered region.
- Let top hBN and bottom hBN fully cover graphene, including the Hall-bar arms/probe regions that must remain encapsulated.
- Let the graphite bottom gate cover the entire patterned graphene Hall-bar footprint, including the channel body and all Hall-bar arms/probe regions. Graphene outside that footprint may remain uncovered by graphite.
- Expose only the intended graphite gate contact tab from hBN coverage, on the single gate side; do not accidentally expose graphite under graphene transport contacts or on any other side.
- Keep the gate side clear of graphene-contact metal, routes, and landings. Route all 10 graphene Hall-bar edge contacts on the other three sides; move any graphene terminal that the default map places on the gate side (by default the source current lead moves from the left edge to the top-edge spare-center endpoint).
- Keep edge-contact metal, gate-contact metal, and local connection metal electrically separated unless they intentionally share a net. Gate metal to any graphene-contact metal: keep at least `3.0 um` clearance (wider than the `1.0 um` route-to-route default) because of the gate bias.
- Do not apply ReSe2 split-gate, source/drain finger, or vertical overhang defaults to this device.

## Template-Backed Initial Designs

When `agents/design_agent/output/new_electrodes_500um_samplearea.GDS` exists, treat it as the fixed imported peripheral electrode/routing circuit for graphene QH initial designs.

For requests that use the complete defaults in this skill, reuse the verified ideal design declared in `references/default-ideal-design.json`. It is the successful `run_187` edge-contact Hall-bar layout with all 20 checks passing. Materialize its GDS, screenshot, result, and provenance into the active numeric run and skip a new Claude/KLayout ideal-design execution. Real-flake selection and real-flake adaptation are still required. If the human explicitly asks for different ideal geometry, terminal topology, contact style, layer policy, or peripheral template, set `force_ideal_redesign=true` or `reuse_default_ideal_design=false` and generate a new ideal design instead.

- Import and preserve `agents/design_agent/output/new_electrodes_500um_samplearea.GDS` before drawing the central device.
- Read [references/graphene-qh-template-profile.json](references/graphene-qh-template-profile.json) before probing the GDS. It provides the 500 um sample-area bbox, endpoint coordinates, layer roles, bottom-edge reference notes, and the default terminal-to-template endpoint map.
- Draw only central active-device geometry inside the template sample area: Hall-bar graphene, top hBN, bottom hBN, bottom graphite gate, graphite gate contact exposure, graphene edge-contact overlaps, and short local metal links to the imported template endpoints.
- Do not generate new peripheral bond pads, alignment markers, long outer fan-out routes, or replacement outer electrodes unless the human explicitly overrides the template.
- Do not also import `agents/design_agent/output/alignmarker2.GDS` or reuse its 21 pads unless approved guidance explicitly requires both templates.
- Treat the imported template as human/external clean-room electrode geometry. Preserve its original layers and include it in connectivity/non-overlap checks.

## Graphene Electrode Layer Policy

For graphene QH template-backed designs, use one single merged newly drawn electrode layer for all new local metal:

- graphene edge-contact metal
- bottom-graphite-gate pickup/contact-exposure metal
- short local connection metal landing on imported template endpoints

Do not split this new local metal into separate edge-contact, gate-contact, and local-routing/template-endpoint layers. The imported template geometry keeps its original layers, and graphene/hBN/graphite material layers remain separate.

## Bottom Graphite Gate - Single-Side Isolation and T-Contact

Route the graphite bottom-gate contact away from every graphene edge contact to remove the gate-to-contact short risk.

- The T applies only to the gate electrode (the graphite gate metal pickup). The graphene mesa/Hall-bar shape, the hBN flakes, and the graphite gate body are unchanged - keep them exactly as the rest of this skill specifies.
- Dedicate one side of the sample area to the gate. Default gate side: the left template edge. Expose the graphite only on that side, as a single tab clear of both hBN layers.
- The gate side carries the gate net only. No graphene-contact metal, route, or landing may enter the gate side between the exposed graphite tab and its template endpoint.
- Move any graphene terminal off the gate side: by default the source current lead exits the top edge (spare-center endpoint `[-7.5, -299.5]`) instead of the left edge; drain and the 8 voltage probes keep their default right/bottom/top endpoints. The gate keeps its left-edge endpoint `[-250.5, -592.5]`.
- Gate metal to any graphene-contact metal: at least `3.0 um` clearance.
- Draw the gate pickup as a T whose crossbar faces the stack:
  - Crossbar: about `16-24 um` long, `4-6 um` wide, sitting entirely on the exposed graphite next to the main gate body (well over the `1.5 um x 1.5 um` minimum intended-contact area).
  - Stem: about `3-4 um` wide, from the crossbar centre out to the template endpoint, with the standard `5 um` handoff overlap into the endpoint.
  - Neither the crossbar nor the stem may sit under either hBN, and both keep the `3.0 um` gate-to-contact clearance.
- The gate pickup T stays on the single merged new-electrode layer with the edge contacts (per `Graphene Electrode Layer Policy`); it is isolated by geometry, not by a separate layer.

## Real-Flake Adaptation

- Use real graphene, top hBN, bottom hBN, and graphite mask geometry when provided by `candidate_mask_paths_by_role`.
- Real flakes may translate only unless the approved guidance explicitly permits rotation. For the current Qumus graphene flow, keep `rotation_deg=0` and `rotation_allowed=false`.
- Preserve the approved ideal Hall-bar intent while adapting local contacts and hBN/gate coverage to the actual masks. Start from the preferred `10 um x 20 um` Hall channel. If it does not fit a selected graphene mask, perform a bounded translation-and-scale search for a compact Hall channel down to, but never below, `5 um x 10 um` before declaring the graphene unusable. Preserve the requested terminal topology while scaling and verify each actual edge-contact landing; failure to fit the preferred size alone is not layout impossibility.
- Apply the `3.0 um` clearance between gate metal and graphene-contact metal in the metal layout. Do not misapply that clearance as a required graphene margin around the channel or use it to reject an otherwise usable `5 um x 10 um` channel region.
- Keep local connection geometry bounded to the imported template sample-area endpoints.
- Keep the single-side gate isolation and T-contact when adapting to real masks: expose the real graphite on one side only, put the T crossbar on that exposed graphite, and keep all graphene contacts and the `3.0 um` gate clearance on the other sides. Choose the gate side the real graphite flake naturally extends toward; if it is not the left edge, move the affected graphene terminal(s) off that side the same way.
- If a real flake cannot support full graphene coverage by both hBN layers, full graphite-gate coverage of the entire patterned Hall-bar footprint, or exposed graphite on a single contact side clear of the graphene contacts, reject that candidate and choose the next feasible candidate rather than relaxing the physical stack. Do not reject a candidate merely because graphite does not cover unused graphene outside the Hall-bar footprint.

## Verification Checklist

Report these checks in `ideal_device_result.json`, `claude_design_result.json`, or run notes:

- Graphene is monolayer role and has a connected Hall-bar geometry.
- The realized Hall channel reports its width and length, prefers approximately `10 um x 20 um`, and is not smaller than `5 um x 10 um`.
- Top hBN fully covers graphene.
- Bottom hBN fully covers graphene.
- Bottom graphite gate fully covers the entire patterned graphene Hall-bar footprint; graphite coverage of unused graphene outside that footprint is not required.
- Graphite gate pickup lands on exposed graphite not covered by hBN.
- Graphite is exposed on exactly one side of the stack, and the gate pickup is a T (crossbar on exposed graphite plus a narrow stem to the endpoint).
- The gate side carries no graphene-contact metal or routes; gate metal keeps at least `3.0 um` from every graphene-contact metal.
- Each of the 10 graphene terminals has a valid edge-contact/local-template connection.
- The graphite gate terminal is electrically isolated from graphene transport terminals.
- No newly drawn metal intrudes into imported template geometry except intentional endpoint overlaps.
- All same-layer local metal is merged before save/verification.
- Result states `status: ok`, `autonomous_continuation: true`, `human_review_required: false`, `review_gate: null`, and `human_review_gates: []` when required artifacts exist and no blocking geometry, net-mapping, pad/template, or process issue remains unresolved.
