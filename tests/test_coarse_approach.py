"""An absent stamp subject cannot acquire a focus bracket from source texture."""
from test_focus_evidence import focus_context  # noqa: F401

from qumus2.flows.library.primitives import autofocus


def test_default_gray_stamp_focus_does_not_invent_absent_subject_bracket(focus_context):  # noqa: F811
    services, ctx, _ = focus_context
    assert not services.lab.world.stamps["stamp_01"].stack
    # Keep the source plane outside this stamp-plane-only window, as in the
    # original absent-subject case. Source texture must not supply the peak.
    ctx.gate.microscope("microscope").set_focus(1.5)
    autofocus(ctx, 20, half_range=.15, plane="stamp")
    observations = [event.payload for event in services.store.events_after(0, limit=100000)
                    if event.kind == "microscope.focus"]
    assert observations[-1]["plane"] == "stamp"
    assert observations[-1]["focus_metric"]["id"] == "raster-focus-gray-v1"
    assert observations[-1]["focus_interval_mm"] is None
