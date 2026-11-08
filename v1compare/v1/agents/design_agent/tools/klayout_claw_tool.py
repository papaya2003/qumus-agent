"""
KLayoutClaw-compatible tools for the design agent.

KlayoutClaw exposes KLayout through an MCP JSON-RPC endpoint at
http://127.0.0.1:8765/mcp.  These wrappers let Qumus either drive that endpoint
directly or export the current optimized masks into a KLayout layout tab.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Dict, List, Tuple

import cv2
import numpy as np
from smolagents import tool


DEFAULT_KLAYOUT_MCP_URL = "http://127.0.0.1:8765/mcp"

_request_id = 0
_session_id: str | None = None
_active_mcp_url = DEFAULT_KLAYOUT_MCP_URL
_initialized_urls: set[str] = set()


def _set_mcp_url(mcp_url: str | None) -> str:
    global _active_mcp_url
    if mcp_url and mcp_url.strip():
        _active_mcp_url = mcp_url.strip()
    return _active_mcp_url


def _mcp_call(method: str, params: Dict | None = None, timeout: int = 30, mcp_url: str = "") -> Dict:
    """Send a JSON-RPC 2.0 request to the KlayoutClaw MCP endpoint."""
    global _request_id, _session_id

    url = _set_mcp_url(mcp_url)
    _request_id += 1
    payload = {
        "jsonrpc": "2.0",
        "id": _request_id,
        "method": method,
    }
    if params is not None:
        payload["params"] = params

    headers = {"Content-Type": "application/json"}
    if _session_id:
        headers["Mcp-Session-Id"] = _session_id

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            _session_id = response.headers.get("Mcp-Session-Id", _session_id)
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise ConnectionError(
            f"Cannot connect to KLayout MCP server at {url}. "
            "Start KLayout with the KlayoutClaw plugin first."
        ) from exc

    if "error" in data:
        raise RuntimeError(f"KLayout MCP error: {data['error']}")
    return data


def _init_session(timeout: int = 30, mcp_url: str = "") -> Dict:
    url = _set_mcp_url(mcp_url)
    if url in _initialized_urls:
        return {"status": "ok", "message": "session already initialized", "mcp_url": url}

    result = _mcp_call(
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "qumus-design-agent", "version": "0.1"},
        },
        timeout=timeout,
        mcp_url=url,
    )
    _initialized_urls.add(url)
    return result


def _tool_call(tool_name: str, arguments: Dict | None = None, timeout: int = 300, mcp_url: str = "") -> Dict:
    """Call a KlayoutClaw MCP tool and parse its JSON text payload."""
    _init_session(timeout=min(timeout, 30), mcp_url=mcp_url)
    result = _mcp_call(
        "tools/call",
        {"name": tool_name, "arguments": arguments or {}},
        timeout=timeout,
        mcp_url=mcp_url,
    )
    content = result.get("result", {}).get("content", [])
    if not content:
        return result
    text = content[0].get("text", "")
    if not text:
        return result
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"status": "ok", "text": text}


def _parse_json_object(raw_json: str, default: Dict | None = None) -> Dict:
    if not raw_json.strip():
        return dict(default or {})
    parsed = json.loads(raw_json)
    if not isinstance(parsed, dict):
        raise ValueError("Expected a JSON object")
    return parsed


def _parse_layer_spec(layer_spec: str) -> Tuple[int, int]:
    parts = layer_spec.strip().split("/")
    if len(parts) == 1:
        return int(parts[0]), 0
    if len(parts) == 2:
        return int(parts[0]), int(parts[1])
    raise ValueError(f"Invalid layer spec: {layer_spec!r}")


def _default_layer_map(materials: List[str], electrodes: List[str]) -> Dict[str, str]:
    layer_map: Dict[str, str] = {}
    for idx, material in enumerate(materials, start=1):
        layer_map[material] = f"{idx}/0"
    for idx, electrode in enumerate(electrodes, start=20):
        layer_map[electrode] = f"{idx}/0"
    return layer_map


def _mask_to_polygons_um(
    mask: np.ndarray,
    pixel_size_um: float,
    max_points_per_polygon: int,
    invert_y: bool,
) -> List[List[List[float]]]:
    """Convert the center-priority target component to polygon coordinates."""
    mask01, _ = _center_priority_mask_component(mask)
    contours, _ = cv2.findContours(mask01, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    height = mask01.shape[0]
    polygons: List[List[List[float]]] = []

    for contour in contours:
        if len(contour) < 3:
            continue
        epsilon = max(1.0, 0.0025 * cv2.arcLength(contour, True))
        approx = cv2.approxPolyDP(contour, epsilon, True)
        points = approx.reshape(-1, 2)
        if len(points) < 3:
            continue

        if len(points) > max_points_per_polygon:
            step = int(np.ceil(len(points) / max_points_per_polygon))
            points = points[::step]
            if len(points) < 3:
                continue

        poly: List[List[float]] = []
        for x_px, y_px in points:
            x_um = float(x_px) * pixel_size_um
            y_source = (height - float(y_px)) if invert_y else float(y_px)
            y_um = y_source * pixel_size_um
            poly.append([x_um, y_um])
        polygons.append(poly)

    return polygons


def _center_priority_mask_component(
    mask: np.ndarray, tie_tolerance_px: float = 50.0
) -> tuple[np.ndarray, Dict[str, object]]:
    """Return one connected component, using area only for near-center ties."""
    array = np.asarray(mask)
    if array.ndim == 3:
        foreground = np.any(array > 10, axis=2).astype(np.uint8)
    elif array.ndim == 2:
        foreground = (array > 0).astype(np.uint8)
    else:
        raise ValueError(f"mask must be 2D or 3D, got shape {array.shape!r}")
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(foreground, 8)
    if count <= 1:
        raise ValueError("No foreground connected component found in mask")
    height, width = foreground.shape
    center_x, center_y = width / 2.0, height / 2.0
    candidates: List[Dict[str, object]] = []
    for label in range(1, count):
        area = int(stats[label, cv2.CC_STAT_AREA])
        if area < 1:
            continue
        centroid_x, centroid_y = float(centroids[label][0]), float(centroids[label][1])
        distance = float(np.hypot(centroid_x - center_x, centroid_y - center_y))
        candidates.append(
            {
                "label": label,
                "area_px": area,
                "centroid_px": [centroid_x, centroid_y],
                "distance_to_image_center_px": distance,
            }
        )
    if not candidates:
        raise ValueError("No usable foreground connected component found in mask")
    nearest = min(float(item["distance_to_image_center_px"]) for item in candidates)
    tied = [
        item
        for item in candidates
        if float(item["distance_to_image_center_px"]) <= nearest + float(tie_tolerance_px)
    ]
    selected = max(tied, key=lambda item: int(item["area_px"]))
    selected_mask = (labels == int(selected["label"])).astype(np.uint8)
    metadata = dict(selected)
    metadata.update(
        {
            "nearest_center_distance_px": nearest,
            "center_tie_tolerance_px": float(tie_tolerance_px),
            "selection_rule": "center_distance_first_area_only_for_near_center_tie",
            "candidate_count": len(candidates),
        }
    )
    return selected_mask, metadata


def _default_output_dir() -> str:
    from common.logic.stacking_run_dirs import get_stacking_run_dirs

    return get_stacking_run_dirs(create_new=False, sample_label="design")["sample_dir"]


@tool
def check_klayout_mcp(mcp_url: str = DEFAULT_KLAYOUT_MCP_URL) -> str:
    """
    Check whether the KlayoutClaw MCP server is reachable.

    Args:
        mcp_url: KlayoutClaw MCP endpoint, usually http://127.0.0.1:8765/mcp.

    Returns:
        JSON status payload reporting whether Qumus can connect to KLayout.
    """
    try:
        init_result = _init_session(timeout=5, mcp_url=mcp_url)
    except Exception as exc:
        return json.dumps(
            {
                "status": "unavailable",
                "mcp_url": mcp_url,
                "message": str(exc),
                "next_step": "Open KLayout with the KlayoutClaw plugin, then retry.",
            },
            ensure_ascii=False,
            indent=2,
        )

    return json.dumps(
        {
            "status": "ok",
            "mcp_url": mcp_url,
            "initialize": init_result.get("result", init_result),
        },
        ensure_ascii=False,
        indent=2,
    )


@tool
def run_klayout_mcp_tool(
    tool_name: str,
    arguments_json: str = "{}",
    timeout_seconds: int = 300,
    mcp_url: str = DEFAULT_KLAYOUT_MCP_URL,
) -> str:
    """
    Call any KlayoutClaw MCP tool by name.

    Args:
        tool_name: KlayoutClaw tool name, for example "execute_script",
            "auto_route", "evaluate_design", or "screenshot".
        arguments_json: JSON object passed as the tool arguments.
        timeout_seconds: Request timeout in seconds.
        mcp_url: KlayoutClaw MCP endpoint.

    Returns:
        JSON result returned by the KlayoutClaw tool.
    """
    try:
        arguments = _parse_json_object(arguments_json)
        result = _tool_call(tool_name, arguments, timeout=timeout_seconds, mcp_url=mcp_url)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"status": "error", "tool": tool_name, "message": str(exc)}, ensure_ascii=False, indent=2)


@tool
def create_klayout_layout(
    name: str = "QUMUS_DESIGN",
    dbu: float = 0.001,
    mcp_url: str = DEFAULT_KLAYOUT_MCP_URL,
) -> str:
    """
    Create a new KLayout layout tab through KlayoutClaw.

    Args:
        name: Top cell name for the new layout.
        dbu: Database unit in microns.
        mcp_url: KlayoutClaw MCP endpoint.

    Returns:
        JSON creation status from KLayout.
    """
    try:
        result = _tool_call("create_layout", {"name": name, "dbu": dbu}, timeout=60, mcp_url=mcp_url)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False, indent=2)


@tool
def execute_klayout_script(
    code: str,
    timeout_seconds: int = 300,
    mcp_url: str = DEFAULT_KLAYOUT_MCP_URL,
) -> str:
    """
    Execute Python/pya code in the active KLayout tab via KlayoutClaw.

    Args:
        code: Python code to execute inside KLayout's pya interpreter.
        timeout_seconds: Request timeout in seconds.
        mcp_url: KlayoutClaw MCP endpoint.

    Returns:
        JSON result returned by the KlayoutClaw execute_script tool.
    """
    try:
        result = _tool_call("execute_script", {"code": code}, timeout=timeout_seconds, mcp_url=mcp_url)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False, indent=2)


@tool
def save_klayout_layout(
    filepath: str,
    format: str = "GDS2",
    mcp_url: str = DEFAULT_KLAYOUT_MCP_URL,
) -> str:
    """
    Save the active KLayout layout through KlayoutClaw.

    Args:
        filepath: Output GDS or OASIS file path.
        format: Layout format, usually "GDS2" or "OASIS".
        mcp_url: KlayoutClaw MCP endpoint.

    Returns:
        JSON save status from KLayout.
    """
    try:
        result = _tool_call("save_layout", {"filepath": filepath, "format": format}, timeout=120, mcp_url=mcp_url)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False, indent=2)


@tool
def capture_klayout_screenshot(
    filepath: str = "",
    width: int = 1280,
    height: int = 900,
    zoom_box_json: str = "",
    mcp_url: str = DEFAULT_KLAYOUT_MCP_URL,
) -> str:
    """
    Capture a KLayout viewport screenshot through KlayoutClaw.

    Args:
        filepath: Optional output PNG path. If empty, writes into the active
            Qumus design output folder.
        width: Screenshot width in pixels.
        height: Screenshot height in pixels.
        zoom_box_json: Optional JSON array [x1, y1, x2, y2] in microns.
        mcp_url: KlayoutClaw MCP endpoint.

    Returns:
        JSON screenshot status from KLayout.
    """
    try:
        if not filepath:
            output_dir = _default_output_dir()
            os.makedirs(output_dir, exist_ok=True)
            filepath = os.path.join(output_dir, "klayout_screenshot.png")

        args: Dict[str, object] = {"filepath": filepath, "width": width, "height": height}
        if zoom_box_json.strip():
            zoom_box = json.loads(zoom_box_json)
            if not isinstance(zoom_box, list) or len(zoom_box) != 4:
                raise ValueError("zoom_box_json must be a JSON array [x1, y1, x2, y2]")
            args["zoom_box"] = [float(v) for v in zoom_box]

        result = _tool_call("screenshot", args, timeout=120, mcp_url=mcp_url)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False, indent=2)


@tool
def export_optimized_masks_to_klayout(
    pixel_size_um: float,
    layer_map_json: str = "",
    layout_name: str = "QUMUS_STACK",
    max_points_per_polygon: int = 96,
    invert_y: bool = True,
    save_gds_path: str = "",
    mcp_url: str = DEFAULT_KLAYOUT_MCP_URL,
) -> str:
    """
    Export the current optimized design masks into a KLayout layout via KlayoutClaw.

    Args:
        pixel_size_um: Microns per mask pixel for converting image coordinates
            into KLayout coordinates.
        layer_map_json: Optional JSON object mapping layer names to specs, e.g.
            {"graphene": "1/0", "hBN": "2/0", "contact_electrode": "20/0"}.
        layout_name: Top cell name for the new KLayout layout.
        max_points_per_polygon: Maximum contour vertices to keep per polygon.
        invert_y: Convert image y-down coordinates to KLayout y-up coordinates.
        save_gds_path: Optional path to save the generated layout as GDS2.
        mcp_url: KlayoutClaw MCP endpoint.

    Returns:
        JSON payload with export counts, layer map, and optional GDS path.
    """
    from agents.design_agent.tools.layout_tool import get_agent_state

    state = get_agent_state()
    optimized_masks = state.get("optimized_masks")
    if not optimized_masks:
        return json.dumps(
            {
                "status": "error",
                "message": "No optimized masks are available. Run optimize_design first.",
            },
            ensure_ascii=False,
            indent=2,
        )

    if pixel_size_um <= 0:
        return json.dumps({"status": "error", "message": "pixel_size_um must be > 0"}, ensure_ascii=False, indent=2)
    if max_points_per_polygon < 3:
        return json.dumps(
            {"status": "error", "message": "max_points_per_polygon must be at least 3"},
            ensure_ascii=False,
            indent=2,
        )

    materials = list(state.get("material_names", []))
    electrodes = list(state.get("electrode_names", []))
    layer_map = _default_layer_map(materials, electrodes)
    layer_map.update(_parse_json_object(layer_map_json))

    export_layers = []
    missing_layers = []
    for name, mask in optimized_masks.items():
        if name not in layer_map:
            missing_layers.append(name)
            continue
        layer, datatype = _parse_layer_spec(str(layer_map[name]))
        polygons = _mask_to_polygons_um(mask, pixel_size_um, max_points_per_polygon, invert_y)
        export_layers.append(
            {
                "name": name,
                "layer": layer,
                "datatype": datatype,
                "polygons": polygons,
            }
        )

    if not export_layers:
        return json.dumps(
            {
                "status": "error",
                "message": "No masks matched the layer map.",
                "layer_map": layer_map,
                "missing_layers": missing_layers,
            },
            ensure_ascii=False,
            indent=2,
        )

    script_payload = json.dumps(export_layers)
    layout_name_literal = json.dumps(layout_name)
    script = f"""
layout = _layout
view = _layout_view
top = _top_cell
if layout is None or view is None or top is None:
    view, layout, top = _get_or_create_view()
dbu = layout.dbu
layout_name = {layout_name_literal}
layers = json.loads(r'''{script_payload}''')
inserted = {{}}
for item in layers:
    li = layout.layer(int(item["layer"]), int(item["datatype"]))
    count = 0
    for poly in item["polygons"]:
        pts = [pya.Point(int(round(x / dbu)), int(round(y / dbu))) for x, y in poly]
        if len(pts) >= 3:
            top.shapes(li).insert(pya.Polygon(pts))
            count += 1
    inserted[item["name"]] = {{
        "layer": f"{{item['layer']}}/{{item['datatype']}}",
        "polygons": count,
    }}
_refresh_view()
result = {{"status": "ok", "layout_name": layout_name, "inserted": inserted}}
"""

    try:
        create_result = _tool_call("create_layout", {"name": layout_name, "dbu": 0.001}, timeout=60, mcp_url=mcp_url)
        script_result = _tool_call("execute_script", {"code": script}, timeout=300, mcp_url=mcp_url)
        save_result = None
        if save_gds_path:
            save_result = _tool_call(
                "save_layout",
                {"filepath": save_gds_path, "format": "GDS2"},
                timeout=120,
                mcp_url=mcp_url,
            )
    except Exception as exc:
        return json.dumps(
            {
                "status": "error",
                "message": str(exc),
                "layer_map": layer_map,
                "missing_layers": missing_layers,
            },
            ensure_ascii=False,
            indent=2,
        )

    payload = {
        "status": "ok",
        "layout_name": layout_name,
        "pixel_size_um": pixel_size_um,
        "layer_map": layer_map,
        "missing_layers": missing_layers,
        "create_layout": create_result,
        "execute_script": script_result,
        "save_layout": save_result,
    }
    state["klayout_export"] = payload
    return json.dumps(payload, ensure_ascii=False, indent=2)


__all__ = [
    "DEFAULT_KLAYOUT_MCP_URL",
    "check_klayout_mcp",
    "run_klayout_mcp_tool",
    "create_klayout_layout",
    "execute_klayout_script",
    "save_klayout_layout",
    "capture_klayout_screenshot",
    "export_optimized_masks_to_klayout",
]
