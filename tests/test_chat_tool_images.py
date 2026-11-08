"""Transport-only image adaptation preserves provenance and rejects ambiguity."""

import copy
from types import SimpleNamespace

import pytest

from qumus2.agents.openai_runtime import transport_model_input


def call(ident):
    return {"type": "function_call", "call_id": ident, "name": "inspect_bench", "arguments": "{}"}


def output(ident, image=None):
    return {"type": "function_call_output", "call_id": ident, "output": [
        {"type": "input_text", "text": "Measured 你好🧪"},
        image or {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]}


def adapt(items, api="chat_completions"):
    from agents.run_config import ModelInputData

    return transport_model_input(SimpleNamespace(model_data=ModelInputData(items, "Rules")), api)


def test_duplicate_pixels_keep_one_attachment_and_original_call_association():
    items = [call("one"), call("two"), output("one"), output("two")]
    original = copy.deepcopy(items)
    adapted = adapt(items)
    assert items == original
    assert adapted.instructions == "Rules"
    assert [i.get("type", i.get("role")) for i in adapted.input] == [
        "function_call", "function_call", "function_call_output", "function_call_output", "user"]
    assert "tool_call_id=one" in adapted.input[-1]["content"][0]["text"]
    assert adapted.input[-1]["content"][1] == original[2]["output"][1]
    assert "Same image attached earlier" in adapted.input[3]["output"][-1]["text"]
    assert all(p["type"] == "input_text" for i in adapted.input[2:4] for p in i["output"])
    native = adapt(items, "responses")
    assert native.input[2] == original[2]
    assert len(native.input) == len(items)  # Responses uses its native image tool result.


@pytest.mark.parametrize("items,reason", [
    ([call("same"), call("same"), output("same"), output("same")], "duplicate call ID"),
    ([call("a"), output("a"), output("a")], "unique preceding call"),
    ([output("orphan")], "unique preceding call"),
    ([call("a"), call("b"), output("a")], "incomplete tool-call group"),
    ([call("a"), call("b"), output("a"), {"role": "user", "content": "next"}], "missing results"),
    ([call("a"), call("b"), output("a"), call("c"), output("b")], "overlapping tool-call groups"),
    ([call("a"), output("a", {"type": "input_image", "file_id": "file-private"})], "require Responses"),
])
def test_ambiguous_or_unsupported_tool_image_input_fails_explicitly(items, reason):
    original = copy.deepcopy(items)
    with pytest.raises(ValueError, match=reason):
        adapt(items)
    assert items == original


def test_ordinary_text_and_base64_text_are_not_moved_into_synthetic_messages():
    items = [call("text"), {"type": "function_call_output", "call_id": "text",
                           "output": "data:image/png;base64,AAAA as ordinary text"}]
    assert adapt(items).input == items
