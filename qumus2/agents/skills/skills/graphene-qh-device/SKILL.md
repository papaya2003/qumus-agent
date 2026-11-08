---
name: graphene-qh-device
description: Lay out an explicitly requested hBN-encapsulated graphene quantum Hall / Hall-bar device with edge contacts fabricated after stacking. Do not use this encapsulation recipe for Gr that must directly contact existing bottom electrodes.
---

# Graphene QH device reference

## Registered eight-contact designs

For a registered `device_design_target` or Device Guidance with
`design_contract=device_design_v1`, follow the Designer prompt's
`create_device_design` route: complete observed hBN/Gr outlines and rigid
translations/rotations, whole-Gr hBN coverage and all eight existing contacts.
Preserve hBN/Gr/electrodes/silicon; do not add etching or fabrication steps.
The legacy rectangle, encapsulation and no-rotation defaults below do not apply.

## Legacy rectangle path

Adapted from the v1 design skill of the same name. For tasks without that
eight-contact contract, `submit_design` records rectangles in micrometres,
one layer per flake role of the device plan plus `contact_metal` and
`gate_metal`. This legacy path does not create a verified KLayout artifact.
Code checks roles, channel minimum, hBN coverage and 3.0 um gate-to-contact
clearance; the following defaults apply only to that legacy path.

Values in the device plan always override the defaults below.

## Applicability

Use these defaults only when the accepted guidance requests encapsulation and
subsequent edge-contact fabrication. They do not describe transfer onto existing
bottom contacts: bottom hBN would separate graphene from those electrodes.
For a legacy target whose accepted guidance requires sole Gr on prepatterned
electrodes, transfer that Gr directly onto the designated contacts. Do not add
top hBN, bottom hBN, a graphite gate or fabrication steps to that legacy request.
Chip-centre placement and a successful generic multilayer stack do not establish
contact with the designated electrodes.

## Legacy encapsulation defaults

- Stack, top to bottom: top hBN / graphene (monolayer preferred) / bottom hBN /
  optional bottom graphite gate (graphite, `manylayer`).
- Hall channel: about 10 x 20 um preferred; 5 x 10 um is the usable minimum,
  not the target.
- Standard Hall bar: source and drain at the two ends of the long axis, four
  pairs of voltage probes along the channel, placed symmetrically.
- Top hBN 10-30 nm and bottom hBN 35-40 nm, both covering all of the
  patterned graphene (channel and probe arms).
- Edge contacts, 5 nm Cr / 60-65 nm Au. Metal deposition is an external
  clean-room step, not a lab hardware action.

## Legacy rectangle geometry

- Draw the graphene as one connected Hall-bar body: the channel rectangle on
  the channel role layer, probe arms as further rectangles on the same layer.
- Source and drain contacts span the full channel width at the two ends.
- Keep the channel inside both hBN rectangles. Make each hBN rectangle the
  part of the real flake you rely on, not an idealised large box: check the
  flake size in `get_design_context` and the images.
- With a graphite gate: the gate covers the whole patterned Hall bar and is
  exposed on exactly one side, outside both hBN layers, for its contact. That
  side carries only the gate contact; route every graphene contact to the
  other three sides. Keep gate metal at least 3.0 um from contact metal.
- Real flakes are only translated, not rotated (rotation is not available on
  the stacker yet).

## Fitting flakes to the legacy rectangle path

Start from 10 x 20 um. If it does not fit the selected graphene, try smaller
channels and other positions down to, but never below, 5 x 10 um, keeping the
terminal layout. Not fitting the preferred size is not a reason to give up.
Only when no channel of at least 5 x 10 um fits with full hBN coverage (and
full gate coverage, if there is a gate) call `report_design_infeasible`, and
name the roles that failed and what you tried.

## Report

Say the channel size you used, whether it is the preferred or a reduced size,
and any risk you see in the images (bubbles, cracks, folds near the channel).
