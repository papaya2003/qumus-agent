"""Blocked measured-region overlays; analytic shapes, no jobs or native calls."""
import copy
import json
import xml.etree.ElementTree as ET

import pytest
from test_device_design_pose_screen import VERIFICATION
from test_imported_hallbar_target import declared_import

from qumus2.agents.klayout import write_measured_region_diagnostic
from qumus2.core.polygons import rectangle
from qumus2.core.regions import region_dict
from qumus2.records.device_design import diagnose_measured_region_layout


def inputs():
    result = []
    for material, size in (("hBN", (.12, .14)), ("Gr", (.06, .08))):
        result.append({"flake_id": material, "material": material,
            "source_frame": "declared_unit_image_frame_not_observed_material",
            "placement_reference": "Explicit analytic test transform; no registration claim",
            "region_mm": region_dict({"components": [{"exterior": rectangle(*size),
                "holes": [rectangle(.002, .002, (-.05, -.06))] if material == "hBN" else []}]}),
            "placement": {"flake_id": material, "translation_mm": [.5, .5], "rotation_deg": 0.},
            "evidence": {"scope": "analytic unit data, not qualified sources"}})
    verification = copy.deepcopy(VERIFICATION)
    verification["refind_flake"]["centre_tolerance_um"] = 1.
    verification["vision_control"]["residual_rotation_deg"] = 1.
    return result, verification


def test_geometric_pass_cannot_qualify_diagnostic_and_unknown_budgets_stay_unknown():
    sources, verification = inputs()
    before = copy.deepcopy(sources)
    report = diagnose_measured_region_layout(declared_import(), sources, verification=verification)
    assert report["nominal"]["status"] == report["pose_only"]["status"] == "pass"
    assert report["quantized_pose_only"]["minimum_headroom_um"] > 0
    assert report["admission_status"] == "blocked"
    assert all(s["measurement_bound_mm"] is None and s["registration_bound_mm"] is None
               for s in report["source_pose_errors"])
    assert report["policy"]["translation_um"] == report["policy"]["rotation_deg"] == 1.
    assert len(report["native_overlay_payload"]["layers"][2]["polygons_dbu"]) == 34
    assert len(report["nominal"]["constraints"]) == 11
    assert sources == before


def test_full_contact_ends_and_whole_gr_holes_cannot_be_waived_by_area():
    sources, verification = inputs()
    sources[0]["region_mm"]["components"][0]["holes"] = [rectangle(.002, .002)]
    sources[1]["region_mm"]["components"][0]["exterior"] = rectangle(.021, .042)
    report = diagnose_measured_region_layout(declared_import(), sources, verification=verification)
    assert "whole_gr_coverage" in report["nominal"]["failed_constraints"]
    assert report["nominal"]["minimum_headroom_um"] is None
    contact = next(c for c in report["nominal"]["constraints"] if c["id"] == "contact_containment:voltage_left_1")
    assert 0 < contact["intersection_area_um2"] < contact["required_area_um2"]
    assert contact["clearance_um"] is None and contact["status"] == "fail"


@pytest.mark.parametrize("field", ["source_frame", "placement_reference"])
def test_missing_coordinate_reference_refuses(field):
    sources, verification = inputs()
    del sources[0][field]
    with pytest.raises(ValueError, match="explicit"):
        diagnose_measured_region_layout(declared_import(), sources, verification=verification)


def test_evidence_writer_keeps_holes_all_metal_and_refuses_overwrite_without_native_call(tmp_path, monkeypatch):
    source_bytes = b"UNIT ONLY; NOT ACTUAL GDS"
    source = tmp_path/"original.gds"
    source.write_bytes(source_bytes)
    sources, verification = inputs()
    def no_native(*args, **kwargs):
        pytest.fail("Diagnostic preparation must not call a native API")
    monkeypatch.setattr("qumus2.agents.klayout._Client", no_native)
    output = tmp_path/"diagnostic"
    saved = write_measured_region_diagnostic(output, declared_import(), sources,
        source_gds=source, verification=verification)
    assert saved["admission_status"] == "blocked" and saved["native_status"] == "not_executed"
    report = json.loads((output/"diagnostic.json").read_text())
    assert len(report["native_overlay_payload"]["layers"][0]["region_components_dbu"][0]["holes"]) == 1
    root = ET.fromstring((output/"overlay.svg").read_text())
    paths = root.findall(".//{http://www.w3.org/2000/svg}path")
    assert sum("data-metal-part" in p.attrib for p in paths) == 34
    assert sum("data-contact" in p.attrib for p in paths) == 8
    assert all(p.attrib["fill-rule"] == "evenodd" for p in paths if "data-source" in p.attrib)
    assert "NOT AN ADMITTED DESIGN" in (output/"overlay.svg").read_text()
    assert "QUMUS_DIAGNOSTIC_ONLY" in (output/"native-script.py").read_text()
    with pytest.raises(FileExistsError):
        write_measured_region_diagnostic(output, declared_import(), sources, source_gds=source, verification=verification)
