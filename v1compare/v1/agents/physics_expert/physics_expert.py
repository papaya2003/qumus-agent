import re
import os
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    import gradio as gr
except ImportError:  # pragma: no cover - only needed for the optional UI entry point.
    gr = None

from dotenv import load_dotenv
from common.llm_config import DEFAULT_MODEL_ID, get_api_base_for_model, get_api_key_for_model, resolve_model_id

load_dotenv()


def _litellm_completion(**kwargs):
    import litellm

    litellm.drop_params = True
    return litellm.completion(**kwargs)

USE_WEB = False
DEFAULT_PHYSICS_MODEL = DEFAULT_MODEL_ID or None

SYSTEM_PROMPT = """
You are an expert in 2D quantum materials and van der Waals device fabrication.
Your task is to design fabrication-ready device stacks and to advise on material choice.

Question-type routing:
- If the user asks which materials the lab could explore / which material to use
  for a research goal (e.g. "advance in 2D semiconductor transistors"), answer with
  a RANKED LIST of candidate materials. For each: name, one-line reason it fits the
  goal, key property numbers (mobility, gap, on/off, contact behavior) with units,
  fabrication considerations given the current lab setup if provided, and a source
  URL when you searched. Do NOT force this into the Option A/B/C layer-stack format.
  Base the reasoning on physics and literature, not on a pre-decided answer.
  Inventory/DB neutrality (STRICT): rank and justify every candidate on physics and
  published evidence ONLY. If a CURRENT LAB SETUP block is provided, use it solely
  to note whether the fabrication route is Qumus-executable vs. needs a human/
  external step. NEVER cite inventory or database state as a reason a material is
  suitable or preferred — do not write "already in DB", "code already in DB",
  "just needs a crystal", "raw carrier already staged", or similar. This applies to
  every material including ReSe2: the presence of a ReSe2 raw-carrier code or
  catalogued flakes is NOT evidence that ReSe2 is the right choice and must not be
  mentioned as such.
  Capability tag = one of qumus_candidate / human_provided / external_fab /
  literature_reference, assessed PER MATERIAL over its full path (exfoliation +
  handling + the fab its device needs). It is NOT a blanket label: an
  air-sensitive material (needs glovebox / inert atmosphere / immediate
  encapsulation — CrI3, NbSe2, black phosphorus, Fe3GeTe2, Cr2Ge2Te6, WTe2,
  air-sensitive MPX3 magnets, …) is `human_provided`, even though it can be
  mechanically exfoliated. `qumus_candidate` is only for air-stable materials
  Qumus can exfoliate + optically search + dry-stack unattended. Put the tag
  token on every row; never write "all candidates are qumus_candidate". Keep one
  consistent column layout for the whole list.
- If the user asks for a device stack / layout, use the layer-stack format below.

Behavior rules:
1. If the user's description is insufficient, FIRST ask concise clarification questions.
2. For device-stack requests, once information is sufficient, ALWAYS generate 2–3 alternative plans, strictly using the following format:

Option A:
Layer 1 (bottom): <material>, <requests>
Layer 2: <material>, <requests>
Layer 3: <material>, <requests>
...
Reason: <brief rationale>

Option B:
Layer 1 (bottom): <material>, <requests>
Layer 2: <material>, <requests>
...
Reason: <brief rationale>

3. Reasons must be 1–2 sentences explaining fabrication logic, electrical motivation, or typical use case.
4. If one option is most standard/robust, append:
   "Recommended default: Option X"
5. The user may add constraints; update plans accordingly.
6. Keep the style concise, lab-protocol-like, fabrication-oriented.
"""

FORCE_OPTIONS_PROMPT = """
The user did not provide additional information. 
Please proceed with reasonable assumptions and generate 2-3 alternative device plans now.
Make typical/standard assumptions for any missing details and clearly state what assumptions you made.
"""

CHAT_HISTORY_FILE = "chat_history.json"

########################################
# Helper Functions
########################################


def save_chat_history(messages):
    with open(CHAT_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)


def load_chat_history():
    if os.path.exists(CHAT_HISTORY_FILE):
        with open(CHAT_HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def call_model(messages, model=None, use_web=None):
    """Call model via LiteLLM."""
    if use_web is None:
        use_web = USE_WEB

    # Keep the same signature; web_search tool is not used for Gemini in this branch.
    _ = use_web
    resolved_model = resolve_model_id(model or DEFAULT_PHYSICS_MODEL)
    api_key = get_api_key_for_model(resolved_model)
    api_base = get_api_base_for_model(resolved_model)
    if not api_key:
        raise RuntimeError("QUMUS_API_KEY not set.")
    completion_kwargs: dict[str, object] = {
        "model": resolved_model,
        "api_key": api_key,
        "messages": messages,
    }
    if api_base:
        completion_kwargs["api_base"] = api_base
    resp = _litellm_completion(**completion_kwargs)
    return (resp.choices[0].message.content or "").strip()


def has_options(text):
    return bool(re.search(r"Option\s+[A-Z]:", text))


def extract_single_option(full_text, opt):
    pattern = r"(Option\s+" + re.escape(opt) + r":[\s\S]*?)(?=\nOption\s+[A-Z]:|\nRecommended|\Z)"
    m = re.search(pattern, full_text, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return None


def guess_default_option(full_text):
    m = re.search(r"Recommended default:\s*Option\s+([A-Z])", full_text)
    if m:
        return m.group(1).upper()
    return "A"


def extract_all_options(full_text):
    """Extract all options from model response."""
    options = {}
    for opt in ["A", "B", "C", "D", "E"]:
        text = extract_single_option(full_text, opt)
        if text:
            options[opt] = text
    return options


########################################
# Gradio UI Functions
########################################


def format_chat_history(messages):
    """Convert messages to Gradio chatbot format."""
    chat_display = []
    for msg in messages:
        if msg["role"] == "user" and msg["content"] != FORCE_OPTIONS_PROMPT:
            chat_display.append((msg["content"], None))
        elif msg["role"] == "assistant":
            if chat_display and chat_display[-1][1] is None:
                chat_display[-1] = (chat_display[-1][0], msg["content"])
            else:
                chat_display.append((None, msg["content"]))
    return chat_display


def user_submit(user_message, chat_history, messages_state):
    """Handle user message submission."""
    if not user_message.strip():
        return "", chat_history, messages_state, gr.update(), gr.update(visible=False), ""

    # Initialize messages if empty
    if not messages_state:
        messages_state = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Add user message
    messages_state.append({"role": "user", "content": user_message})
    chat_history.append((user_message, None))

    # Get model response
    reply = call_model(messages_state)
    messages_state.append({"role": "assistant", "content": reply})
    chat_history[-1] = (user_message, reply)

    # Save history
    save_chat_history(messages_state)

    # Check if options are available
    if has_options(reply):
        options = extract_all_options(reply)
        default_opt = guess_default_option(reply)
        choices = list(options.keys())
        return "", chat_history, messages_state, gr.update(choices=choices, value=default_opt, visible=True), gr.update(visible=True), reply
    else:
        return "", chat_history, messages_state, gr.update(visible=False), gr.update(visible=False), reply


def auto_proceed(chat_history, messages_state, last_reply):
    """Auto-generate plans when user doesn't provide info."""
    if not messages_state:
        messages_state = [{"role": "system", "content": SYSTEM_PROMPT}]
        return chat_history, messages_state, gr.update(visible=False), gr.update(visible=False), ""

    if has_options(last_reply):
        # Auto-select default option
        chosen = guess_default_option(last_reply)
        selected_text = extract_single_option(last_reply, chosen)
        final_msg = f"[AUTO-SELECTED: Option {chosen}]\n\n{selected_text}"
        chat_history.append((None, final_msg))
        return chat_history, messages_state, gr.update(visible=False), gr.update(visible=False), ""
    else:
        # Generate plans with assumptions
        messages_state.append({"role": "user", "content": FORCE_OPTIONS_PROMPT})
        chat_history.append(("[Auto-proceeding with default assumptions...]", None))

        reply = call_model(messages_state)
        messages_state.append({"role": "assistant", "content": reply})
        chat_history[-1] = ("[Auto-proceeding with default assumptions...]", reply)

        save_chat_history(messages_state)

        if has_options(reply):
            options = extract_all_options(reply)
            default_opt = guess_default_option(reply)
            choices = list(options.keys())
            return chat_history, messages_state, gr.update(choices=choices, value=default_opt, visible=True), gr.update(visible=True), reply
        else:
            return chat_history, messages_state, gr.update(visible=False), gr.update(visible=False), reply


def select_option(option, chat_history, messages_state, last_reply):
    """Handle option selection."""
    if not option or not last_reply:
        return chat_history, messages_state, gr.update(visible=False), gr.update(visible=False), ""

    selected_text = extract_single_option(last_reply, option)
    if selected_text:
        final_msg = f"[SELECTED: Option {option}]\n\n{selected_text}"
        chat_history.append((f"I choose Option {option}", final_msg))
        save_chat_history(messages_state)

    return chat_history, messages_state, gr.update(visible=False), gr.update(visible=False), ""


def clear_chat():
    """Clear chat and start fresh."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    save_chat_history(messages)
    return [], messages, gr.update(visible=False), gr.update(visible=False), ""


def load_previous():
    """Load previous chat history."""
    existing = load_chat_history()
    if existing and len(existing) > 1:
        chat_display = format_chat_history(existing)
        # Get last reply for option detection
        last_reply = ""
        for msg in reversed(existing):
            if msg["role"] == "assistant":
                last_reply = msg["content"]
                break

        if has_options(last_reply):
            options = extract_all_options(last_reply)
            default_opt = guess_default_option(last_reply)
            choices = list(options.keys())
            return chat_display, existing, gr.update(choices=choices, value=default_opt, visible=True), gr.update(visible=True), last_reply
        return chat_display, existing, gr.update(visible=False), gr.update(visible=False), last_reply
    return [], [{"role": "system", "content": SYSTEM_PROMPT}], gr.update(visible=False), gr.update(visible=False), ""


########################################
# Build Gradio Interface
########################################

if __name__ == "__main__":
    if gr is None:
        raise RuntimeError("gradio is required to launch the physics expert UI.")

    with gr.Blocks(title="2D Quantum Device Planner", theme=gr.themes.Soft()) as demo:
        gr.Markdown("# 2D Quantum Device Planner")
        gr.Markdown("Design fabrication-ready van der Waals device stacks with AI assistance.")

        # State variables
        messages_state = gr.State([{"role": "system", "content": SYSTEM_PROMPT}])
        last_reply_state = gr.State("")

        # Chat interface
        chatbot = gr.Chatbot(label="Conversation", height=450, show_copy_button=True)

        # Option selection (hidden by default)
        with gr.Row(visible=False) as option_row:
            option_dropdown = gr.Radio(choices=["A", "B", "C"], label="Select an Option", visible=False)
            select_btn = gr.Button("Confirm Selection", variant="primary")

        # User input
        with gr.Row():
            user_input = gr.Textbox(
                placeholder="Describe your device structure or goal... (Press Enter to send, or leave empty and click 'Auto Proceed')", label="Your Message", scale=4, lines=2
            )
            submit_btn = gr.Button("Send", variant="primary", scale=1)

        # Action buttons
        with gr.Row():
            auto_btn = gr.Button("Auto Proceed (Use Defaults)", variant="secondary")
            clear_btn = gr.Button("New Conversation", variant="stop")
            load_btn = gr.Button("Load Previous Chat")

        gr.Markdown("---")
        gr.Markdown("**Tips:** Describe your device goal (e.g., 'I want to test a monolayer WSe2 transistor'). The AI will ask clarifying questions or generate options.")

        # Event handlers
        submit_btn.click(user_submit, inputs=[user_input, chatbot, messages_state], outputs=[user_input, chatbot, messages_state, option_dropdown, option_row, last_reply_state])

        user_input.submit(user_submit, inputs=[user_input, chatbot, messages_state], outputs=[user_input, chatbot, messages_state, option_dropdown, option_row, last_reply_state])

        auto_btn.click(auto_proceed, inputs=[chatbot, messages_state, last_reply_state], outputs=[chatbot, messages_state, option_dropdown, option_row, last_reply_state])

        select_btn.click(
            select_option, inputs=[option_dropdown, chatbot, messages_state, last_reply_state], outputs=[chatbot, messages_state, option_dropdown, option_row, last_reply_state]
        )

        clear_btn.click(clear_chat, outputs=[chatbot, messages_state, option_dropdown, option_row, last_reply_state])

        load_btn.click(load_previous, outputs=[chatbot, messages_state, option_dropdown, option_row, last_reply_state])

    # Launch the interface
    demo.launch()
