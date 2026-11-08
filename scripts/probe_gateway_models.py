"""Small paid probe: compare gateway models on overload rate, latency and cost.

Each sample is one OpenAI Agents SDK run with one function tool (the model
must call the tool, then answer), so it checks tool calling as well as the
gateway. Every HTTP attempt is tallied by status code, including the OpenAI
client's own retries, so the 503 rate is per attempt.

Spends API credit. Stops before a sample whose worst-case cost would exceed
the per-model share of `--budget`.

    python scripts/probe_gateway_models.py --budget 1 --samples 15 \
        --out runs/checks/model-probe.json openai/gpt-6-astra qwen-token-plan/qwen3.8-max

Reads QUMUS_OPENAI_API_KEY / QUMUS_OPENAI_BASE_URL from .env; prints no key.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from collections import Counter
from pathlib import Path

import yaml

PROMPT = ("A sample on the transfer stage needs a temperature check. Call read_stage_temperature "
          "once, then reply with one short sentence giving the value in degrees Celsius.")


PRICE_OVERRIDE: dict[str, tuple[float, float]] = {}


def prices(model: str) -> tuple[float, float]:
    if model in PRICE_OVERRIDE:
        return PRICE_OVERRIDE[model]
    table = yaml.safe_load((Path(__file__).resolve().parents[1] / "qumus2/config/"
                            "model_context_limits.yaml").read_text())["quoted_prices_usd_per_million"]
    p = table.get(model)
    if not p:
        raise SystemExit(f"no quoted price for {model}; refusing to spend without one")
    return float(p["input"]), float(p["output"])


IMAGE_PROMPT = ("Call capture_microscope_image once. It returns a microscope frame with a few "
                "red dots on a grey background. Reply with only the number of red dots, as digits.")


def dots_png(n: int) -> str:
    """A 320x240 PNG data URL with `n` red dots, so the answer can be checked."""
    import base64
    import io

    from PIL import Image, ImageDraw
    im = Image.new("RGB", (320, 240), (128, 128, 128))
    d = ImageDraw.Draw(im)
    for k in range(n):
        x, y = 40 + 60 * k, 60 + 50 * (k % 3)
        d.ellipse((x, y, x + 30, y + 30), fill=(220, 20, 20))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


async def probe(model: str, api: str, creds, samples: int, cap: float, max_out: int,
                image: bool = False, image_in_user: bool = False) -> dict:
    import httpx
    from agents import (
        Agent,
        ModelSettings,
        OpenAIChatCompletionsModel,
        OpenAIResponsesModel,
        Runner,
        ToolOutputImage,
        ToolOutputText,
        function_tool,
    )
    from openai import AsyncOpenAI, DefaultAsyncHttpxClient

    pin, pout = prices(model)
    statuses: Counter = Counter()
    latencies: list[float] = []
    started: dict[int, float] = {}

    async def on_request(request: httpx.Request) -> None:
        started[id(request)] = time.monotonic()

    async def on_response(response: httpx.Response) -> None:
        statuses[response.status_code] += 1
        t = started.pop(id(response.request), None)
        if t is not None and response.status_code == 200:
            latencies.append(time.monotonic() - t)

    http = DefaultAsyncHttpxClient(event_hooks={"request": [on_request], "response": [on_response]})
    client = AsyncOpenAI(api_key=creds.api_key, base_url=creds.base_url, max_retries=3,
                         timeout=120.0, http_client=http)

    @function_tool
    def read_stage_temperature() -> str:
        """Read the transfer stage temperature sensor."""
        return "Stage temperature: 24.6 C"

    truth = {"n": 0}

    @function_tool
    def capture_microscope_image() -> list:
        """Capture one frame from the microscope camera."""
        return [ToolOutputText(text="Microscope frame (PNG)."),
                ToolOutputImage(image_url=dots_png(truth["n"]))]

    llm = (OpenAIResponsesModel(model, client) if api == "responses"
           else OpenAIChatCompletionsModel(model, client))
    agent = Agent(name="probe", instructions="You operate lab instruments through tools.",
                  tools=[capture_microscope_image if image else read_stage_temperature], model=llm,
                  model_settings=ModelSettings(max_tokens=max_out))
    if image_in_user:
        agent = Agent(name="probe", instructions="You look at microscope frames.", model=llm,
                      model_settings=ModelSettings(max_tokens=max_out))
    spent, ok, tool_ok, errors, wall, answers = 0.0, 0, 0, [], [], []
    worst = 2 * ((4000 if image else 2000) * pin + max_out * pout) / 1e6  # two calls per sample
    for i in range(samples):
        if spent + worst > cap:
            errors.append(f"stopped before sample {i}: budget share ${cap:.2f}")
            break
        t0, answered = time.monotonic(), statuses.get(200, 0)
        try:
            truth["n"] = 3 + i % 3
            if image_in_user:
                # The same frame placed directly in the user message, no tool.
                prompt = [{"role": "user", "content": [
                    {"type": "input_text", "text": "This is a microscope frame with a few red dots on a "
                     "grey background. Reply with only the number of red dots, as digits."},
                    {"type": "input_image", "image_url": dots_png(truth["n"]), "detail": "auto"}]}]
            else:
                prompt = IMAGE_PROMPT if image else PROMPT
            res = await Runner.run(agent, prompt, max_turns=4)
        except Exception as e:  # noqa: BLE001 - tallied, not fatal
            errors.append(f"{type(e).__name__}: {str(e)[:160]}")
            # A failed sample that got any answer counts at worst case: unknown
            # usage must not look free. Refused-only samples cost nothing.
            if statuses.get(200, 0) > answered:
                spent += worst
            continue
        finally:
            wall.append(time.monotonic() - t0)
        u = res.context_wrapper.usage
        spent += (u.input_tokens * pin + u.output_tokens * pout) / 1e6
        ok += 1
        called = any(getattr(it, "type", "") == "tool_call_item" for it in res.new_items)
        expect = str(truth["n"]) if image or image_in_user else "24.6"
        tool_ok += bool((called or image_in_user) and expect in str(res.final_output))
        answers.append({"expected": expect, "tool_called": called, "answer": str(res.final_output)[:300]})
    await client.close()
    attempts = sum(statuses.values())
    return {"model": model, "api": api, "task": "image-in-user" if image_in_user else "image" if image else "text", "samples_done": ok, "samples_tool_correct": tool_ok,
            "sample_errors": errors, "http_attempts": attempts, "http_status": dict(statuses),
            "rate_503": round(statuses.get(503, 0) / attempts, 3) if attempts else None,
            "rate_429": round(statuses.get(429, 0) / attempts, 3) if attempts else None,
            "latency_s_median": round(statistics.median(latencies), 2) if latencies else None,
            "sample_wall_s_median": round(statistics.median(wall), 2) if wall else None,
            "cost_usd_est": round(spent, 4), "answers": answers,
            "cost_per_sample_usd": round(spent / ok, 5) if ok else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="+")
    ap.add_argument("--budget", type=float, required=True, help="total USD cap across models")
    ap.add_argument("--samples", type=int, default=15)
    ap.add_argument("--max-output-tokens", type=int, default=600)
    ap.add_argument("--api", choices=("responses", "chat_completions"), default=None,
                    help="default: the .env setting")
    ap.add_argument("--image", action="store_true",
                    help="the tool returns an image (red dots to count) instead of text")
    ap.add_argument("--image-in-user", action="store_true",
                    help="put the frame in the user message instead of a tool result (no tool)")
    ap.add_argument("--price", action="append", default=[], metavar="MODEL=IN,OUT",
                    help="assumed USD per million tokens for a model with no quoted price")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    from qumus2.agents.openai_credentials import load_openai_credentials
    creds = load_openai_credentials()
    if creds is None:
        raise SystemExit("QUMUS_OPENAI_API_KEY is not set in .env")
    for spec in args.price:
        name, rates = spec.split("=", 1)
        pin, pout = (float(x) for x in rates.split(","))
        PRICE_OVERRIDE[name] = (pin, pout)
    for m in args.models:
        prices(m)
    share = args.budget / len(args.models)
    out = {"base_url": creds.base_url, "budget_usd": args.budget, "results": []}
    for m in args.models:
        api = args.api or creds.api
        r = asyncio.run(probe(m, api, creds, args.samples, share, args.max_output_tokens,
                              args.image or args.image_in_user, args.image_in_user))
        if not r["samples_done"] and api == "responses" and not r["http_status"].get(503):
            # Some gateway backends only speak Chat Completions; try once more with that.
            r = asyncio.run(probe(m, "chat_completions", creds, args.samples, share,
                                  args.max_output_tokens, args.image or args.image_in_user,
                                  args.image_in_user))
        out["results"].append(r)
        print(json.dumps(r), flush=True)
    out["total_cost_usd_est"] = round(sum(r["cost_usd_est"] for r in out["results"]), 4)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2))
    print("total_cost_usd_est", out["total_cost_usd_est"], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
