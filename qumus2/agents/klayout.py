"""Bounded KlayoutClaw MCP adapter; only trusted typed geometry becomes code.

One atomic new-view call owns generation, save/reopen and the native viewport.
Historical batch artifacts remain readable; fresh creation never falls back.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import socket
import threading
import time
from html import escape
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from qumus2.records.device_design import (
    DeviceDesignSpec,
    ImportedHallBarTarget,
    KLayoutMCPArtifact,
    KLayoutMCPReceipt,
    KLayoutPythonArtifact,
    KLayoutPythonReceipt,
    design_directory,
    diagnose_measured_region_layout,
    layout_payload,
    validate_imported_target_files,
    validate_klayout_mcp_receipt,
    validate_klayout_python_receipt,
)
from qumus2.records.geometry import geometry_digest

DEFAULT_ENDPOINT = "http://localhost:8767/mcp"
PROTOCOL = "2025-03-26"
ATOMIC_PROFILE = "atomic-new-layout-v1"
MAX_BODY = 1_000_000
_TRANSPORT_LOCK = threading.Lock()


class KLayoutError(RuntimeError):
    pass


class _DeadlineSocket(socket.socket):
    """Every underlying receive/send uses the remaining total budget, including headers."""
    def __init__(self, family, remaining):
        super().__init__(family, socket.SOCK_STREAM)
        self._remaining = remaining

    def recv_into(self, *args, **kwargs):
        self.settimeout(self._remaining())
        return super().recv_into(*args, **kwargs)

    def sendall(self, *args, **kwargs):
        self.settimeout(self._remaining())
        return super().sendall(*args, **kwargs)


class _DeadlineConnection(http.client.HTTPConnection):
    def __init__(self, host, port, remaining):
        self._remaining = remaining
        super().__init__(host, port, timeout=remaining())

    def connect(self):
        # Numeric loopback avoids an unbounded hostname-resolution phase.
        host = "127.0.0.1" if self.host == "localhost" else self.host
        self.sock = _DeadlineSocket(socket.AF_INET6 if host == "::1" else socket.AF_INET, self._remaining)
        self.sock.settimeout(self._remaining())
        self.sock.connect((host, self.port))


def endpoint() -> str:
    value = os.environ.get("QUMUS_KLAYOUT_MCP_URL", DEFAULT_ENDPOINT)
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.port != 8767 or parsed.path != "/mcp" or parsed.query or parsed.fragment
            or parsed.username is not None or parsed.password is not None):
        raise KLayoutError("KLayout MCP must use the existing loopback port8767 /mcp endpoint.")
    return value


class _Client:
    def __init__(self, url: str, deadline: float, directory: Path):
        self.url, self.deadline, self.directory = url, deadline, directory
        self.session = None
        self.sequence = 0
        self.calls = []

    def remaining(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise KLayoutError("KLayout MCP deadline expired; remote completion is unknown. No automatic retry.")
        return left

    def call(self, method: str, params: dict) -> dict:
        self.sequence += 1
        request = {"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params}
        raw = json.dumps(request, allow_nan=False).encode()
        if len(raw) > MAX_BODY:
            raise KLayoutError("Generated KLayout MCP request exceeds the bridge's bounded body limit.")
        entry = {"request": request}
        self.calls.append(entry)
        parsed = urlsplit(self.url)
        connection = _DeadlineConnection(parsed.hostname, parsed.port, self.remaining)
        try:
            headers = {"Content-Type": "application/json", "Accept": "application/json",
                       "MCP-Protocol-Version": PROTOCOL}
            if self.session:
                headers["Mcp-Session-Id"] = self.session
            connection.request("POST", "/mcp", raw, headers)
            response = connection.getresponse()
            if response.status != 200:
                raise KLayoutError(f"KLayout MCP returned HTTP{response.status}; redirects/fallback are refused.")
            self.session = response.getheader("Mcp-Session-Id", self.session)
            chunks, size = [], 0
            while True:
                left = self.remaining()
                # HTTP/1.0 may detach connection.sock while the response owns it.
                sock = connection.sock or getattr(getattr(response.fp, "raw", None), "_sock", None)
                if sock is not None:
                    sock.settimeout(left)
                chunk = response.read1(min(65536, MAX_BODY + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_BODY:
                    raise KLayoutError("KLayout MCP response exceeds the bounded body limit.")
            body = b"".join(chunks)
            envelope = json.loads(body, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
            entry["response"] = envelope
            if (not isinstance(envelope, dict) or envelope.get("jsonrpc") != "2.0"
                    or type(envelope.get("id")) is not int or envelope.get("id") != self.sequence or "error" in envelope
                    or not isinstance(envelope.get("result"), dict)):
                raise KLayoutError("KLayout MCP JSON-RPC response is invalid or reports an error.")
            return envelope["result"]
        except (OSError, http.client.HTTPException, ValueError) as exc:
            entry["error"] = type(exc).__name__
            raise KLayoutError("KLayout MCP request could not finish; retained attempt is unaccepted and remote completion may be unknown.") from exc
        finally:
            connection.close()
            (self.directory / "mcp-transport.json").write_text(json.dumps(self.calls, indent=2), encoding="utf-8")

    def tool(self, name: str, arguments: dict) -> dict:
        result = self.call("tools/call", {"name": name, "arguments": arguments})
        content = result.get("content")
        if (result.get("isError") or not isinstance(content, list) or len(content) != 1
                or not isinstance(content[0], dict) or content[0].get("type") != "text"
                or not isinstance(content[0].get("text"), str)):
            raise KLayoutError("KLayout MCP tool failed or returned an unexpected content envelope.")
        try:
            value = json.loads(content[0]["text"])
        except (TypeError, ValueError) as exc:
            raise KLayoutError("KLayout MCP tool returned invalid JSON text.") from exc
        if (not isinstance(value, dict) or value.get("protections_applied")
                or value.get("status") not in {"ok", "verified"}):
            raise KLayoutError("KLayout MCP tool did not verify cleanly; server recovery cannot authorize acceptance.")
        return value

    def initialize(self) -> dict:
        result = self.call("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "qumus-device-designer", "version": "1"}})
        server = result.get("serverInfo", {})
        if (not isinstance(server, dict) or result.get("protocolVersion") != PROTOCOL or server.get("name") != "KlayoutClaw"
                or not isinstance(server.get("version"), str) or not server["version"]):
            raise KLayoutError("KLayout MCP server identity/protocol is unsupported.")
        tools = self.call("tools/list", {}).get("tools")
        if not isinstance(tools, list):
            raise KLayoutError("KLayout MCP tool schemas are missing.")
        found = [t for t in tools if isinstance(t, dict) and t.get("name") == "execute_script"]
        schema = found[0].get("inputSchema", {}) if len(found) == 1 else {}
        if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
            raise KLayoutError("KLayout MCP execute_script schema is malformed.")
        atomic = schema["properties"].get("atomic_new_layout", {})
        if not isinstance(atomic, dict) or not isinstance(atomic.get("properties"), dict):
            raise KLayoutError("KLayout MCP lacks atomic-new-layout-v1; no batch fallback.")
        props, required = atomic["properties"], atomic.get("required")
        code, name, dbu = (schema["properties"].get("code"), props.get("name"), props.get("dbu"))
        if (atomic.get("type") != "object" or atomic.get("additionalProperties") is not False
                or not isinstance(required, list) or any(not isinstance(v, str) for v in required)
                or set(required) != {"name", "dbu"}
                or not all(isinstance(v, dict) for v in (code, name, dbu))
                or name.get("type") != "string" or dbu.get("type") != "number"
                or not isinstance(atomic.get("description"), str)
                or ATOMIC_PROFILE not in atomic["description"] or code.get("type") != "string"):
            raise KLayoutError("KLayout MCP lacks atomic-new-layout-v1; update the existing bridge before designing. No batch fallback.")
        return server


def _script(payload: dict, directory: Path, nonce: str, marker: str, zoom: list[float],
            source_gds: Path | None = None) -> str:
    """All substitutions are data literals generated from validated typed records."""
    return f'''import hashlib
import pathlib

payload = json.loads({json.dumps(payload)!r})
root = pathlib.Path({str(directory)!r})
nonce, marker = {nonce!r}, {marker!r}
zoom = {zoom!r}
source_gds = {str(source_gds) if source_gds is not None else None!r}

def canonical(points):
    points = list(points)
    rotations = [tuple(points[i:]+points[:i]) for i in range(len(points))]
    reverse = list(reversed(points))
    rotations += [tuple(reverse[i:]+reverse[:i]) for i in range(len(reverse))]
    return min(rotations)

def polygon_points(polygon):
    return canonical([(point.x, point.y) for point in polygon.each_point_hull()])

def item_polygons(item):
    return item["polygons_dbu"] if "polygons_dbu" in item else [item["points_dbu"]]

def item_regions(item):
    if "region_components_dbu" in item:
        return [(part["exterior"], part["holes"]) for part in item["region_components_dbu"]]
    return [(points, []) for points in item_polygons(item)]

def native_polygon(component):
    exterior, holes = component
    polygon = pya.Polygon([pya.Point(x, y) for x, y in exterior])
    for ring in holes:
        polygon.insert_hole([pya.Point(x, y) for x, y in ring])
    if polygon.area() <= 0:
        raise ValueError("DBU quantization collapsed a required polygon region")
    return polygon

if "source_target" in payload:
    source_path = pathlib.Path(source_gds) if source_gds is not None else None
    receipt = payload["source_target"]["import_receipt"]
    if (source_path is None or source_path.is_symlink()
            or hashlib.sha256(source_path.read_bytes()).hexdigest() != receipt["source_sha256"]):
        raise ValueError("Original imported GDS bytes differ")
    original = pya.Layout()
    original.read(str(source_path))
    if (original.dbu != receipt["dbu_um"] or original.cells() != 1
            or len(list(original.each_top_cell())) != 1
            or original.top_cell().name != receipt["source_cell"]
            or list(original.top_cell().each_inst())):
        raise ValueError("Original imported GDS cell/DBU differs")
    source_layer = original.find_layer(*receipt["source_layer"])
    if source_layer is None:
        raise ValueError("Original imported electrode layer is absent")
    shapes = list(original.top_cell().shapes(source_layer).each())
    primitives = payload["source_target"]["primitives"]
    if len(shapes) != len(primitives) or len(shapes) != 34:
        raise ValueError("Original imported primitive inventory differs")
    for index, (shape, part) in enumerate(zip(shapes, primitives)):
        if index != part["index"]:
            raise ValueError("Original imported primitive index differs")
        if part["kind"] == "path" and shape.is_path():
            path = shape.path
            points = [[point.x, point.y] for point in path.each_point()]
            if (points != part["points_dbu"] or path.width != part["width_dbu"]
                    or path.bgn_ext != part["bgn_ext"] or path.end_ext != part["end_ext"]):
                raise ValueError("Original imported path semantics differ")
            polygon = path.polygon()
        elif part["kind"] == "box" and shape.is_box():
            box = shape.box
            if [box.left, box.bottom, box.right, box.top] != part["box_dbu"]:
                raise ValueError("Original imported box semantics differ")
            polygon = pya.Polygon(box)
        elif part["kind"] == "polygon" and shape.is_polygon():
            polygon = shape.polygon
            if polygon_points(polygon) != canonical([tuple(point) for point in part["points_dbu"]]):
                raise ValueError("Original imported polygon semantics differ")
        else:
            raise ValueError("Original imported primitive type differs")
        if polygon_points(polygon) != canonical([tuple(point) for point in part["outline_dbu"]]):
            raise ValueError("Original converted metal polygon differs")
view = _layout_view
layout = _layout
top = _top_cell
mw = pya.Application.instance().main_window()
if (view is None or view is not mw.current_view() or view.active_cellview().layout() is not layout
        or layout.dbu != payload["dbu_um"] or layout.cells() != 1 or top.name != marker
        or list(layout.layer_indices())):
    raise ValueError("Atomic new-view ownership/empty geometry differs")
view_identity = nonce + ":" + str(id(view))
view_index = next(i for i in range(mw.views()) if mw.view(i) is view)
top.name = payload["top_cell"]
for item in payload["layers"]:
    for component in item_regions(item):
        top.shapes(layout.layer(item["layer"], 0)).insert(native_polygon(component))
output_name = "diagnostic.gds" if payload.get("diagnostic_only") else "device.gds"
path, tmp = root / output_name, root / (output_name + ".tmp")
if path.exists() or tmp.exists() or path.is_symlink() or tmp.is_symlink():
    raise ValueError("Generated output must be fresh")
options = pya.SaveLayoutOptions()
options.format = "GDS2"
layout.write(str(tmp), options)
os.replace(str(tmp), str(path))

def verify(candidate):
    if (candidate.dbu != payload["dbu_um"] or candidate.cells() != 1
            or len(list(candidate.each_top_cell())) != 1
            or candidate.top_cell().name != payload["top_cell"]
            or list(candidate.top_cell().each_inst())
            or len(list(candidate.layer_indices())) != len(payload["layers"])
            or {{(candidate.get_info(li).layer, candidate.get_info(li).datatype)
                 for li in candidate.layer_indices()}} != {{(item["layer"], 0) for item in payload["layers"]}}):
        raise ValueError("Saved GDS cell/layer inventory differs")
    for item in payload["layers"]:
        li = candidate.find_layer(item["layer"], 0)
        cell = candidate.top_cell()
        shapes = list(cell.shapes(li).each())
        components = item_regions(item)
        expected = pya.Region()
        for component in components:
            expected.insert(native_polygon(component))
        actual = pya.Region(cell.begin_shapes_rec(li))
        if (("region_components_dbu" not in item and len(shapes) != len(components))
                or any(not (shape.is_polygon() or shape.is_box()) for shape in shapes)
                or not (expected ^ actual).is_empty()):
            raise ValueError("Saved GDS polygon differs: " + item["name"])
        actual_parts = sorted(polygon_points(shape.polygon if shape.is_polygon() else pya.Polygon(shape.box))
                              for shape in shapes)
        if ("source_target" in payload and "region_components_dbu" not in item
                and actual_parts != sorted(canonical([tuple(point) for point in points])
                                           for points in item_polygons(item))):
            raise ValueError("Saved GDS primitive decomposition differs: " + item["name"])
reopened = pya.Layout()
reopened.read(str(path))
verify(reopened)
# Replace only our newly owned cellview with the bytes actually reopened.
view.load_layout(str(path), False)
_layout_view = view
_layout = view.active_cellview().layout()
_top_cell = _layout.top_cell()
view.active_cellview().cell = _top_cell
verify(_layout)
if mw.view(view_index) is not view or mw.current_view() is not view:
    raise ValueError("Owned native view changed")
view.add_missing_layers()
styles = {{1: (0x55AAFF, "hBN"), 2: (0x66CC66, "Gr")}}
by_layer = {{item["layer"]: item["name"] for item in payload["layers"]}}
it = view.begin_layers()
while not it.at_end():
    lp = it.current()
    if lp.source_layer in by_layer and lp.source_datatype == 0:
        color, label = styles.get(lp.source_layer, (0xEE8844, by_layer[lp.source_layer]))
        lp.name = label + " " + by_layer[lp.source_layer]
        lp.fill_color, lp.frame_color = color, color
        lp.visible, lp.transparent = True, True
        view.set_layer_properties(it, lp)
    it.next()
view.zoom_box(pya.DBox(*zoom))
view.save_image(str(root / "native.png"), 1280, 960)
result = {{"status": "verified", "payload": payload, "nonce": nonce,
    "gds_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    "image_sha256": hashlib.sha256((root / "native.png").read_bytes()).hexdigest(),
    "view_identity": view_identity, "view_index": view_index, "zoom_box_um": zoom,
    "layer_names": [item["name"] for item in payload["layers"]]}}
'''


def create_artifact(store, spec: DeviceDesignSpec, *, timeout_s: float = 30) -> KLayoutMCPArtifact:
    """One owned native view and immutable generated files; no automatic retry."""
    if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or timeout_s <= 0:
        raise KLayoutError("KLayout MCP timeout must be finite and positive.")
    deadline = time.monotonic() + timeout_s
    if not _TRANSPORT_LOCK.acquire(timeout=timeout_s):
        raise KLayoutError("Another Qumus design owns the KLayout MCP transport; no layout created.")
    try:
        directory = design_directory(store, spec.design_id)
        directory.mkdir(parents=True, exist_ok=False)
        payload = layout_payload(spec)
        (directory / "geometry.json").write_text(json.dumps(payload), encoding="utf-8")
        source_gds = validate_imported_target_files(store, spec.target) if isinstance(spec.target, ImportedHallBarTarget) else None
        if "viewport_box_um" in payload:
            zoom = payload["viewport_box_um"]
        else:
            points = [point for item in payload["layers"] for point in item["points_dbu"]]
            xs, ys = ([p[axis] * payload["dbu_um"] for p in points] for axis in (0, 1))
            margin = max(max(xs)-min(xs), max(ys)-min(ys)) * .12 + 1
            zoom = [min(xs)-margin, min(ys)-margin, max(xs)+margin, max(ys)+margin]
        nonce = uuid4().hex
        marker = "QUMUS_OWNED_" + nonce
        script = _script(payload, directory, nonce, marker, zoom, source_gds)
        (directory / "roundtrip.py").write_text(script, encoding="utf-8")
        client = _Client(endpoint(), deadline, directory)
        server = client.initialize()
        result = client.tool("execute_script", {"code": script,
            "atomic_new_layout": {"name": marker, "dbu": payload["dbu_um"]}})
        if (result.get("status") != "verified" or result.get("payload") != payload
                or result.get("nonce") != nonce or result.get("stdout") or result.get("stderr")
                or result.get("atomic_new_layout") != {"profile": ATOMIC_PROFILE, "name": marker, "dbu": .001}
                or not str(result.get("view_identity", "")).startswith(nonce + ":")
                or result.get("zoom_box_um") != zoom):
            raise KLayoutError("KLayout MCP owned-view/readback confirmation differs; design remains unaccepted.")
        receipt = KLayoutMCPReceipt(design_id=spec.design_id, nonce=nonce, endpoint=client.url,
            server_version=server["version"], geometry_sha256=geometry_digest(payload),
            script_sha256=hashlib.sha256(script.encode()).hexdigest(),
            gds_sha256=result["gds_sha256"], image_sha256=result["image_sha256"],
            view_identity=result["view_identity"], view_index=result["view_index"],
            layer_names=result["layer_names"], zoom_box_um=zoom)
        raw = json.dumps(receipt.model_dump(mode="json"), sort_keys=True, allow_nan=False).encode()
        (directory / "verified.json").write_bytes(raw)
        artifact = KLayoutMCPArtifact(path=str(directory / "device.gds"), sha256=receipt.gds_sha256,
            geometry_sha256=receipt.geometry_sha256, receipt_sha256=hashlib.sha256(raw).hexdigest())
        if hashlib.sha256((directory / "device.gds").read_bytes()).hexdigest() != artifact.sha256:
            raise KLayoutError("KLayout MCP saved GDS bytes differ from readback.")
        validate_klayout_mcp_receipt(directory, spec, artifact)
        client.remaining()
        return artifact
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise KLayoutError("KLayout MCP saved artifact/native receipt is missing or invalid; attempt retained without acceptance.") from exc
    finally:
        _TRANSPORT_LOCK.release()


BACKENDS = ("mcp", "python")


def selected_backend() -> str:
    """Explicit operator choice; an unavailable choice fails, it never switches."""
    value = os.environ.get("QUMUS_KLAYOUT_BACKEND", "mcp").strip().lower()
    if value not in BACKENDS:
        raise KLayoutError("QUMUS_KLAYOUT_BACKEND must be 'mcp' (KlayoutClaw, default) or 'python' (klayout module).")
    return value


def create_design_artifact(store, spec: DeviceDesignSpec):
    """Create the design GDS through the explicitly selected backend."""
    if selected_backend() == "python":
        return create_artifact_python(store, spec)
    return create_artifact(store, spec)


# Headless stand-ins for the few GUI objects the generated script touches. The
# geometry, save, reopen and verification code is the identical generated text.
class _CellView:
    def __init__(self, layout):
        self._layout, self.cell = layout, None

    def layout(self):
        return self._layout


class _LayerIterator:
    def at_end(self):
        return True


class _HeadlessView:
    def __init__(self, db, layout, image_size=(1280, 960)):
        self._db, self._cellview, self.zoom, self.image = db, _CellView(layout), None, None
        self._size = image_size

    def active_cellview(self):
        return self._cellview

    def load_layout(self, path, _add_cellview):
        layout = self._db.Layout()
        layout.read(path)
        self._cellview = _CellView(layout)

    def add_missing_layers(self):
        pass

    def begin_layers(self):
        return _LayerIterator()

    def set_layer_properties(self, *_args):
        pass

    def zoom_box(self, box):
        self.zoom = (box.left, box.bottom, box.right, box.top)

    def save_image(self, path, width, height):
        _render_preview(self._cellview.layout(), self.zoom, Path(path), (width, height))


class _HeadlessWindow:
    def __init__(self, view):
        self._view = view

    def views(self):
        return 1

    def view(self, index):
        return self._view if index == 0 else None

    def current_view(self):
        return self._view


class _HeadlessApplication:
    def __init__(self, window):
        self._window = window

    def instance(self):
        return self

    def main_window(self):
        return self._window


class _PyaShim:
    def __init__(self, db, window):
        self._db, self.Application = db, _HeadlessApplication(window)

    def __getattr__(self, name):
        return getattr(self._db, name)


_PREVIEW_COLORS = {1: (40, 122, 192), 2: (48, 145, 83), 3: (200, 117, 37)}


def _render_preview(layout, zoom, path: Path, size):
    """Deterministic raster of every saved layer in the device viewport."""
    import klayout.db as db
    from PIL import Image, ImageDraw

    width, height = size
    x0, y0, x1, y1 = zoom
    scale = min(width/(x1-x0), height/(y1-y0))
    ox, oy = (width-(x1-x0)*scale)/2, (height-(y1-y0)*scale)/2

    def pixel(point):
        return (ox+(point.x*layout.dbu-x0)*scale, height-(oy+(point.y*layout.dbu-y0)*scale))

    image = Image.new("RGBA", (width, height), (255, 255, 255, 255))
    top = layout.top_cell()
    for index in sorted(layout.layer_indices(), key=lambda i: -layout.get_info(i).layer):
        number = layout.get_info(index).layer
        color = _PREVIEW_COLORS.get(number, (110, 110, 110))
        mask = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(mask)
        outline = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        edges = ImageDraw.Draw(outline)
        for shape in top.shapes(index).each():
            polygon = shape.polygon if shape.is_polygon() else db.Polygon(shape.box)
            hull = [pixel(p) for p in polygon.each_point_hull()]
            draw.polygon(hull, fill=90 if number == 3 else 110)
            edges.polygon(hull, outline=(*color, 255))
            for hole in range(polygon.holes()):
                ring = [pixel(p) for p in polygon.each_point_hole(hole)]
                draw.polygon(ring, fill=0)
                edges.polygon(ring, outline=(*color, 255))
        image.paste(Image.new("RGBA", (width, height), (*color, 255)), (0, 0), mask)
        image.alpha_composite(outline)
    ImageDraw.Draw(image).text((12, 12), "Headless KLayout preview: blue hBN, green Gr, orange electrode metal", fill=(40, 40, 40, 255))
    image.convert("RGB").save(path, format="PNG")


def create_artifact_python(store, spec: DeviceDesignSpec) -> KLayoutPythonArtifact:
    """Same generated save/reopen/verify script, run by the klayout Python module."""
    try:
        import klayout
        import klayout.db as db
    except ImportError as exc:
        raise KLayoutError("QUMUS_KLAYOUT_BACKEND=python needs the klayout package (pip install -e '.[design]').") from exc
    if not _TRANSPORT_LOCK.acquire(timeout=30):
        raise KLayoutError("Another Qumus design owns the KLayout transport; no layout created.")
    try:
        directory = design_directory(store, spec.design_id)
        directory.mkdir(parents=True, exist_ok=False)
        payload = layout_payload(spec)
        (directory / "geometry.json").write_text(json.dumps(payload), encoding="utf-8")
        source_gds = validate_imported_target_files(store, spec.target) if isinstance(spec.target, ImportedHallBarTarget) else None
        zoom = payload.get("viewport_box_um")
        if zoom is None:
            points = [point for item in payload["layers"] for point in item["points_dbu"]]
            xs, ys = ([p[axis] * payload["dbu_um"] for p in points] for axis in (0, 1))
            margin = max(max(xs)-min(xs), max(ys)-min(ys)) * .12 + 1
            zoom = [min(xs)-margin, min(ys)-margin, max(xs)+margin, max(ys)+margin]
        nonce = uuid4().hex
        marker = "QUMUS_OWNED_" + nonce
        script = _script(payload, directory, nonce, marker, zoom, source_gds)
        (directory / "roundtrip.py").write_text(script, encoding="utf-8")
        layout = db.Layout()
        layout.dbu = payload["dbu_um"]
        top = layout.create_cell(marker)
        view = _HeadlessView(db, layout)
        namespace = {"pya": _PyaShim(db, _HeadlessWindow(view)), "json": json, "os": os,
                     "_layout_view": view, "_layout": layout, "_top_cell": top}
        exec(compile(script, str(directory / "roundtrip.py"), "exec"), namespace)
        result = namespace.get("result") or {}
        if (result.get("status") != "verified" or result.get("payload") != payload
                or result.get("nonce") != nonce or result.get("zoom_box_um") != zoom):
            raise KLayoutError("Headless KLayout readback confirmation differs; design remains unaccepted.")
        os.replace(directory / "native.png", directory / "preview.png")
        receipt = KLayoutPythonReceipt(design_id=spec.design_id, nonce=nonce, klayout_version=klayout.__version__,
            geometry_sha256=geometry_digest(payload), script_sha256=hashlib.sha256(script.encode()).hexdigest(),
            gds_sha256=result["gds_sha256"], image_sha256=result["image_sha256"],
            layer_names=result["layer_names"], zoom_box_um=zoom)
        raw = json.dumps(receipt.model_dump(mode="json"), sort_keys=True, allow_nan=False).encode()
        (directory / "verified.json").write_bytes(raw)
        artifact = KLayoutPythonArtifact(path=str(directory / "device.gds"), sha256=receipt.gds_sha256,
            geometry_sha256=receipt.geometry_sha256, receipt_sha256=hashlib.sha256(raw).hexdigest())
        if hashlib.sha256((directory / "device.gds").read_bytes()).hexdigest() != artifact.sha256:
            raise KLayoutError("Headless KLayout saved GDS bytes differ from readback.")
        validate_klayout_python_receipt(directory, spec, artifact)
        return artifact
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise KLayoutError(f"Headless KLayout artifact is missing or invalid ({exc}); attempt retained without acceptance.") from exc
    finally:
        _TRANSPORT_LOCK.release()


def write_measured_region_diagnostic(directory, target, sources, *, source_gds, verification=None):
    """Save a blocked overlay and native script; never connect to KLayout.

    This prepares reviewable evidence, not a DesignArtifact or native acceptance
    receipt. An operator may separately execute the saved script through the
    existing atomic-new-layout bridge, which independently checks original GDS
    semantics. The script's explicit diagnostic cell/output cannot admit sources.
    """
    report = diagnose_measured_region_layout(target, sources, verification=verification)
    payload = report["native_overlay_payload"]
    source_gds = Path(source_gds).resolve()
    if (not source_gds.is_file()
            or hashlib.sha256(source_gds.read_bytes()).hexdigest() != payload["source_target"]["import_receipt"]["source_sha256"]):
        raise ValueError("Diagnostic overlay requires the exact original imported GDS bytes")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    nonce = uuid4().hex
    marker = "QUMUS_DIAGNOSTIC_"+nonce
    script = _script(payload, directory.resolve(), nonce, marker, payload["viewport_box_um"], source_gds)
    files = {"native-script.py": script.encode(), "overlay.svg": _diagnostic_overlay_svg(report).encode()}
    report["native_preparation"] = {"status": "not_executed", "script": "native-script.py",
        "atomic_new_layout": {"name": marker, "dbu": payload["dbu_um"]},
        "output": "diagnostic.gds", "scope": "Diagnostic source/export check only; no source/design admission"}
    files["diagnostic.json"] = (json.dumps(report, sort_keys=True, indent=2, allow_nan=False)+"\n").encode()
    artifacts = {}
    for name, data in files.items():
        path = directory/name
        path.write_bytes(data)
        artifacts[name] = {"path": str(path.resolve()), "sha256": hashlib.sha256(data).hexdigest()}
    return {"admission_status": "blocked", "native_status": "not_executed", "artifacts": artifacts,
            "input_sha256": report["input_sha256"]}


def _diagnostic_overlay_svg(report):
    """All holes/metal parts retained; viewport follows the device-scale payload."""
    payload = report["native_overlay_payload"]
    x0, y0, x1, y1 = payload["viewport_box_um"]
    width, height = x1-x0, y1-y0
    pieces = ['<svg xmlns="http://www.w3.org/2000/svg" width="1120" height="850" viewBox="0 0 1120 850">',
        '<rect width="1120" height="850" fill="#ffffff"/>',
        '<text x="28" y="34" font-family="sans-serif" font-size="22" fill="#9b3d10">DIAGNOSTIC · NOT AN ADMITTED DESIGN</text>',
        '<text x="28" y="62" font-family="sans-serif" font-size="15">Supplied measured regions · unknown measurement/registration error is not applied</text>',
        f'<svg x="28" y="86" width="1064" height="650" viewBox="{x0} {-y1} {width} {height}" preserveAspectRatio="xMidYMid meet">']
    stroke = max(width, height)/900

    def path(rings):
        commands = []
        for ring in rings:
            commands.append("M "+" L ".join(f"{x*.001:.12g} {-y*.001:.12g}" for x, y in ring)+" Z")
        return " ".join(commands)

    for item in reversed(payload["layers"]):
        color = {1: "#287ac0", 2: "#309153", 3: "#c87525"}[item["layer"]]
        if "region_components_dbu" in item:
            for part in item["region_components_dbu"]:
                data = path((part["exterior"], *part["holes"]))
                pieces.append(f'<path data-source="{escape(item["name"], quote=True)}" d="{data}" fill="{color}" fill-opacity=".28" fill-rule="evenodd" stroke="{color}" stroke-width="{stroke}"/>')
        else:
            for index, polygon in enumerate(item["polygons_dbu"]):
                pieces.append(f'<path data-metal-part="{index}" d="{path((polygon,))}" fill="{color}" fill-opacity=".14" stroke="{color}" stroke-width="{stroke}"/>')
    statuses = {c["id"].removeprefix("contact_containment:"): c["status"]
                for c in report["quantized_pose_only"]["constraints"] if c["id"].startswith("contact_containment:")}
    for contact in payload["contact_regions_mm"]:
        ring = [[x*1e6, y*1e6] for x, y in contact["points_mm"]]
        color = "#a92a28" if statuses[contact["id"]] == "fail" else "#303030"
        pieces.append(f'<path data-contact="{escape(contact["id"], quote=True)}" d="{path((ring,))}" fill="none" stroke="{color}" stroke-width="{stroke*1.4}"/>')
    headroom = report["quantized_pose_only"]["minimum_headroom_um"]
    margin = "undefined: a nominal containment fails" if headroom is None else f"{headroom:.6f} µm"
    policy = report["policy"]
    pieces += ['</svg>',
        f'<text x="28" y="764" font-family="sans-serif" font-size="16">Pose only: {policy["translation_um"]} µm / {policy["rotation_deg"]}° · minimum DBU margin: {escape(margin)}</text>',
        '<text x="28" y="791" font-family="sans-serif" font-size="14">Blue hBN · green Gr · orange original metal (34 parts) · outlined complete contact ends (8)</text>',
        '<text x="28" y="817" font-family="sans-serif" font-size="13">Target axes in µm; device-scale viewport. Full original metal remains in payload/native script. No material or electrical acceptance.</text>',
        '</svg>']
    return "\n".join(pieces)
