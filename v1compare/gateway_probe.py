"""Which models can v1 (smolagents + LiteLLM) reach through the lab gateway?

Run on the Mac with the venv active (the key comes from the Keychain via
~/.zshrc, as for v2). Sends one tiny non-streaming request per candidate and
one tool-call request for each that answers; costs well under one cent.

    python -m v1compare.gateway_probe

Prints OK/FAIL per candidate. The key is never printed.
"""

from __future__ import annotations

import os
import sys

CANDIDATES = [
    # (label, litellm model id, api_base suffix)
    ("GPT-4o (OpenAI-style route)", "openai/{prefix_openai}gpt-4o", "/v1"),
    ("GPT-4o (no prefix)", "openai/gpt-4o", "/v1"),
    ("Claude Opus 5.5 (OpenAI-style route)", "openai/{prefix}claude-opus-5-5", "/v1"),
    ("Claude Opus 5.5 (Anthropic route, Bearer)", "anthropic/{prefix}claude-opus-5-5", ""),
]

TOOL = {"type": "function", "function": {
    "name": "get_temperature", "description": "Read a heater temperature.",
    "parameters": {"type": "object", "properties": {"heater": {"type": "string"}},
                   "required": ["heater"]}}}


def main() -> int:
    key = os.environ.get("QUMUS_ANTHROPIC_API_KEY")
    base = (os.environ.get("QUMUS_ANTHROPIC_BASE_URL") or "").rstrip("/")
    prefix = os.environ.get("QUMUS_ANTHROPIC_MODEL_PREFIX", "")
    if not key or not base:
        print("QUMUS_ANTHROPIC_API_KEY / QUMUS_ANTHROPIC_BASE_URL are not set (see README).")
        return 2
    import litellm

    litellm.drop_params = True
    for label, model, suffix in CANDIDATES:
        mid = model.format(prefix=prefix, prefix_openai=prefix.replace("anthropic/", "openai/"))
        kw = {"model": mid, "api_key": key, "api_base": base + suffix, "timeout": 60,
              "num_retries": 0, "max_tokens": 50}
        if mid.startswith("anthropic/"):
            # the gateway takes Bearer auth only; LiteLLM's Anthropic route sends x-api-key
            kw["extra_headers"] = {"Authorization": f"Bearer {key}"}
        try:
            r = litellm.completion(messages=[{"role": "user", "content": "Say OK."}], **kw)
            text = (r.choices[0].message.content or "").strip()[:40]
            t = litellm.completion(
                messages=[{"role": "user", "content": "What is the exfoliation heater at?"}],
                tools=[TOOL], **kw)
            calls = t.choices[0].message.tool_calls or []
            try:  # smolagents' ToolCallingAgent forces a tool call on every request
                litellm.completion(
                    messages=[{"role": "user", "content": "What is the exfoliation heater at?"}],
                    tools=[TOOL], tool_choice="required", **kw)
                forced = "accepted"
            except Exception as exc:  # noqa: BLE001 - reported, not fatal
                forced = f"refused ({str(exc)[:100]})"
            # smolagents sends message content as a list of text parts; check that
            # the system prompt survives the route in both shapes
            sysmsg = "The code word is PAPAYA-7. Reply with the code word only."
            seen = []
            for content in (sysmsg, [{"type": "text", "text": sysmsg}]):
                try:
                    r2 = litellm.completion(
                        messages=[{"role": "system", "content": content},
                                  {"role": "user", "content": [
                                      {"type": "text", "text": "What is the code word?"}]}],
                        **kw)
                    seen.append("PAPAYA-7" in (r2.choices[0].message.content or ""))
                except Exception as exc:  # noqa: BLE001 - reported, not fatal
                    seen.append(f"error {str(exc)[:80]}")
            print(f"     system prompt kept: as text {seen[0]}, as parts {seen[1]}")
            print(f"OK   {label}: model={mid!r} replied {text!r}; tool call: "
                  f"{calls[0].function.name if calls else 'none'}; forced tool choice {forced}")
        except Exception as exc:  # noqa: BLE001 - report and try the next one
            print(f"FAIL {label}: model={mid!r}: {type(exc).__name__}: {str(exc)[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
