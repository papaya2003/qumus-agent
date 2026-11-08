"""Real KLayout batch save/reopen of analytic pixel regions; no native UI view."""
import ast
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from test_region_device_design import regional_spec

from qumus2.agents.klayout import _script
from qumus2.perception.rgb_material import component_geometry
from qumus2.records.device_design import DeviceDesignSpec, layout_payload


@pytest.mark.parametrize("fill_hole", [False, True])
def test_exact_pixel_regions_survive_native_gds_roundtrip(tmp_path, fill_hole):
    executable = shutil.which("klayout") or "/Applications/klayout.app/Contents/MacOS/klayout"
    if not Path(executable).is_file():
        pytest.skip("KLayout batch binary is unavailable")
    raw = regional_spec().model_dump(mode="json")
    for i, source in enumerate(raw["sources"]):
        mask = np.zeros((200, 200), dtype=bool)
        if i == 0:
            mask[20:180, 20:180] = True
            mask[70:130, 20:25] = False  # Preserve a genuine exterior notch.
            mask[30:35, 30:35] = False
            mask[5:10, 190:195] = True
        else:
            mask[70:130, 70:130] = True
        geometry = component_geometry(mask, {"mm_per_pixel": [.002, .002],
            "u_axis": "+world_x", "v_axis": "-world_y"})
        source["region_mm"] = geometry["region_mm"]
        source["observation"]["region_mm"] = geometry["region_mm"]
    payload = layout_payload(DeviceDesignSpec.model_validate(raw))
    # Reuse the production integer construction and verification functions,
    # without its MCP/native-view mutation blocks.
    source = ast.parse(_script(payload, tmp_path, "analytic", "analytic", [0, 0, 1, 1]))
    names = {"canonical", "polygon_points", "item_polygons", "item_regions", "native_polygon", "verify"}
    functions = ast.unparse(ast.Module(body=[n for n in source.body
        if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[]))
    program = "import pya\nimport json\nfrom pathlib import Path\n"
    program += "payload=json.loads("+repr(json.dumps(payload))+")\n"+functions+"\n"
    program += "layout=pya.Layout();layout.dbu=payload['dbu_um'];top=layout.create_cell(payload['top_cell'])\n"
    program += "for item in payload['layers']:\n    for component in item_regions(item):\n        top.shapes(layout.layer(item['layer'],0)).insert(native_polygon(component))\n"
    if fill_hole:
        program += "part=payload['layers'][0]['region_components_dbu'][0]\n"
        program += "for ring in part['holes']:\n    top.shapes(layout.layer(1,0)).insert(pya.Polygon([pya.Point(x,y) for x,y in ring]))\n"
    program += "path=Path("+repr(str(tmp_path/"analytic-regions.gds"))+");layout.write(str(path));reopened=pya.Layout();reopened.read(str(path))\n"
    if fill_hole:
        program += "try:\n    verify(reopened)\nexcept ValueError as exc:\n    assert 'Saved GDS polygon differs' in str(exc)\nelse:\n    raise AssertionError('Filled original pixel hole was accepted')\n"
    else:
        program += "verify(reopened)\n"
    path = tmp_path/"roundtrip-control.py"
    path.write_text(program)
    result = subprocess.run([executable, "-b", "-r", str(path)], text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout+result.stderr
    assert (tmp_path/"analytic-regions.gds").is_file()
