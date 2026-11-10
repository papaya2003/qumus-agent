"""Compare raw streamed and non-streamed tool calls. Never executes the tools.

All calls use the selected project model and the shared estimated spend ledger.
Raw SSE deltas and parsed calls are saved locally, without keys or lab data.
Multiple tool calls in one SSE packet are legal; lost indexes or concatenated
JSON arguments are not. A model returning fewer calls makes the test inconclusive.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openai import AsyncOpenAI  # noqa: E402

from qumus2.agents.openai_budget import SpendLedger  # noqa: E402
from qumus2.agents.openai_credentials import load_openai_credentials  # noqa: E402
from qumus2.core.scrub import scrub_text  # noqa: E402

TOOLS = [{"type": "function", "function": {
    "name": name, "description": desc, "parameters": {
        "type": "object", "properties": {field: {"type": "string"}}, "required": [field]}}}
    for name, desc, field in (("view_flake", "Show a flake's images", "flake_id"),
                              ("count_chips", "Count chips by status", "status"))]
PROMPT = ("Make exactly three separate tool calls in this response: view_flake with "
          'flake_id="f1", view_flake with flake_id="f2", and count_chips with status="bare". '
          "Return the tool calls, not prose or code. These are diagnostic mock tools.")


async def main() -> None:
    creds = load_openai_credentials()
    if not creds or creds.input_price is None or creds.output_price is None:
        raise SystemExit("Configure project credentials, model and token rates first")
    folder = Path("runs/checks/stream") / uuid4().hex[:8]
    folder.mkdir(parents=True)
    ledger = SpendLedger(Path(creds.budget_path), creds.stage_budget_usd)
    report = []
    async with AsyncOpenAI(api_key=creds.api_key, base_url=creds.base_url,
                           max_retries=1, timeout=90) as client:
        for streaming, parallel in ((True, True), (False, True), (True, False)):
            reservation = ledger.reserve(str(folder), 0.5, min(20, creds.test_budget_usd))
            result = {"stream": streaming, "parallel_tool_calls": parallel, "deltas": [], "calls": []}
            try:
                response = await client.chat.completions.create(
                    model=creds.model_id, messages=[{"role": "user", "content": PROMPT}],
                    tools=TOOLS, tool_choice="required", parallel_tool_calls=parallel,
                    max_tokens=1536, stream=streaming,
                    **({"stream_options": {"include_usage": True}} if streaming else {}))
                usage = None
                if streaming:
                    calls = {}
                    async for chunk in response:
                        if chunk.usage:
                            usage = chunk.usage
                        for choice in chunk.choices:
                            for call in choice.delta.tool_calls or []:
                                result["deltas"].append({"choice": choice.index, **call.model_dump()})
                                key = (choice.index, call.index)
                                item = calls.setdefault(key, {"index": call.index, "id": "", "name": "", "arguments": ""})
                                item["id"] += call.id or ""
                                if call.function:
                                    item["name"] += call.function.name or ""
                                    item["arguments"] += call.function.arguments or ""
                    result["calls"] = list(calls.values())
                else:
                    usage = response.usage
                    result["calls"] = [{"id": t.id, "name": t.function.name,
                                        "arguments": t.function.arguments}
                                       for t in response.choices[0].message.tool_calls or []]
                if usage:
                    cost = (usage.prompt_tokens * creds.input_price
                            + usage.completion_tokens * creds.output_price) / 1_000_000
                    ledger.settle(reservation, cost)
                    result.update(usage=usage.model_dump(), estimated_usd=cost)
                result["valid_json"] = True
                for call in result["calls"]:
                    try:
                        call["parsed"] = json.loads(call["arguments"])
                    except ValueError:
                        result["valid_json"] = False
                expected = {("view_flake", "f1"), ("view_flake", "f2"), ("count_chips", "bare")}
                actual = {(c["name"], (c.get("parsed") or {}).get("flake_id")
                           or (c.get("parsed") or {}).get("status")) for c in result["calls"]}
                result["all_three_distinct"] = expected == actual and len(result["calls"]) == 3
            except Exception as exc:
                result["error"] = scrub_text(str(exc), [creds.api_key])
            report.append(result)
            (folder / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps({k: v for k, v in result.items() if k != "deltas"}), flush=True)
            await asyncio.sleep(5)
    print("Evidence:", folder / "report.json")


if __name__ == "__main__":
    asyncio.run(main())
