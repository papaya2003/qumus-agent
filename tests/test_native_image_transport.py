"""Native RGB transport must not silently replace photographs with masks."""

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("agents")

from agents.run_config import ModelInputData  # noqa: E402

from qumus2.agents.openai_runtime import tool_output, unique_image_input  # noqa: E402
from qumus2.agents.tooling import ToolResult  # noqa: E402


def test_native_rgb_bytes_survive_transport_and_history_filter(tmp_path):
    paths = []
    originals = []
    for index, name in enumerate(("context.png", "100x.png", "20x.png", "mask.png")):
        payload = bytes([index + 1]) * (6_400_000 if index < 3 else 20_000)
        path = tmp_path / name
        path.write_bytes(payload)
        paths.append(str(path))
        originals.append(payload)
    output = tool_output(ToolResult(status="ok", summary="Images", images=paths))
    urls = [part.image_url for part in output if hasattr(part, "image_url")]
    assert [base64.b64decode(url.split(",", 1)[1]) for url in urls] == originals
    item = {"type": "function_call_output", "call_id": "view",
            "output": [{"type": "input_image", "image_url": url} for url in urls]}
    filtered = unique_image_input(SimpleNamespace(model_data=ModelInputData([item], None)))
    assert filtered.input == [item]
    assert [Path(p).read_bytes() for p in paths] == originals


def test_unavailable_or_omitted_pixels_are_explicit(tmp_path):
    large = tmp_path / "oversize.png"
    with large.open("wb") as stream:
        stream.truncate(8_000_001)
    unsupported = tmp_path / "document.txt"
    unsupported.write_text("not an image")
    small = tmp_path / "small.png"
    small.write_bytes(b"pixels")
    output = tool_output(ToolResult(status="ok", summary="Images", images=[
        str(large), str(tmp_path / "missing.png"), str(unsupported), str(small),
        str(tmp_path / "fifth.png")]))
    messages = "\n".join(part.text for part in output if hasattr(part, "text"))
    for reason in ("8 MB local transport limit", "file unavailable",
                   "unsupported image type", "four-image attachment limit"):
        assert reason in messages
    assert len([part for part in output if hasattr(part, "image_url")]) == 1


def test_optional_transport_downscale_keeps_local_original(tmp_path, monkeypatch):
    from PIL import Image

    big = tmp_path / "tile.png"
    Image.new("RGB", (3560, 2360), (120, 90, 200)).save(big)
    small = tmp_path / "small.png"
    Image.new("RGB", (480, 320), (10, 20, 30)).save(small)
    original = big.read_bytes()
    monkeypatch.setenv("QUMUS_MODEL_IMAGE_MAX_SIDE", "2048")
    output = tool_output(ToolResult(status="ok", summary="Images", images=[str(big), str(small)]))
    urls = [part.image_url for part in output if hasattr(part, "image_url")]
    assert urls[0].startswith("data:image/jpeg;base64,")
    from io import BytesIO
    with Image.open(BytesIO(base64.b64decode(urls[0].split(",", 1)[1]))) as sent:
        assert sent.size == (2048, 1358)
    assert base64.b64decode(urls[1].split(",", 1)[1]) == small.read_bytes()
    assert "downscaled for transport from 3560x2360" in "\n".join(
        part.text for part in output if hasattr(part, "text"))
    assert big.read_bytes() == original
