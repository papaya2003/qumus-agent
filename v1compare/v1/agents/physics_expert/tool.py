from smolagents import Tool
from typing import Optional, List, Dict, Any
from .physics_expert import call_model, SYSTEM_PROMPT, FORCE_OPTIONS_PROMPT, has_options, load_chat_history, save_chat_history


class PhysicsExpertTool(Tool):
    name = "physics_expert"
    description = "Consults an expert in 2D quantum materials and device fabrication. Keeps conversation history to support follow-up questions. Can design device stacks or answer fabrication questions."
    inputs = {
        "query": {"type": "string", "description": "The question or device description to ask the expert."},
        "force_assumptions": {
            "type": "boolean",
            "description": "If true, forces the expert to make reasonable assumptions instead of asking clarification questions.",
            "nullable": True,
            "default": False,
        },
        "use_web": {"type": "boolean", "description": "If true, enables web search for up-to-date information.", "nullable": True, "default": False},
        "clear_history": {"type": "boolean", "description": "If true, clears the conversation history before asking the query.", "nullable": True, "default": False},
    }
    output_type = "string"

    def __init__(self, use_file_history: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.use_file_history = use_file_history
        # Initialize with system prompt
        self.messages: List[Dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]

        # Optionally load from file if configured (to mimic original behavior)
        if self.use_file_history:
            history = load_chat_history()
            if history:
                self.messages = history

    def forward(self, query: str, force_assumptions: bool = False, use_web: bool = False, clear_history: bool = False) -> str:
        # Handle history clearing
        if clear_history:
            self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            if self.use_file_history:
                save_chat_history(self.messages)

        # Add user query to history
        self.messages.append({"role": "user", "content": query})

        # Prefer the Claude Agent SDK backend (real internet search) when the
        # caller asked for web access and the SDK/auth is available.
        response = None
        if use_web:
            try:
                from agents.physics_expert.sdk_agent import run_claude_sdk_physics_expert

                transcript = "\n".join(
                    f"{m['role'].capitalize()}: {m['content']}"
                    for m in self.messages
                    if m["role"] != "system"
                )
                sdk_result = run_claude_sdk_physics_expert(
                    query, system_prompt=SYSTEM_PROMPT, conversation_context=transcript
                )
                if sdk_result.get("status") == "ok" and sdk_result.get("text"):
                    response = str(sdk_result["text"])
                    print("[PhysicsExpert] ✅ Answered via Claude Agent SDK (web search).")
                else:
                    print(
                        "[PhysicsExpert] ⚠️  Web search requested but Claude Agent SDK backend "
                        f"unavailable (status={sdk_result.get('status')!r}: "
                        f"{sdk_result.get('reason', 'unknown')}). Falling back to the chat model "
                        "— answer will NOT be literature-grounded. Set ANTHROPIC_API_KEY to enable it."
                    )
            except Exception as exc:  # pragma: no cover
                print(f"[PhysicsExpert] SDK backend unavailable ({exc}); using chat model.")

        if response is None:
            # Call model with FULL history
            print(f"\n[PhysicsExpert] ⏳ Sending request to chat model (Web Search: {use_web})...")
            response = call_model(self.messages, use_web=use_web)
            print(f"[PhysicsExpert] ✅ Received response from chat model.")

        # Add assistant response to history
        self.messages.append({"role": "assistant", "content": response})

        # Handle automatic "Force Assumptions" logic
        if force_assumptions and not has_options(response):
            # If expert asks for clarification but we want to force assumptions
            print(f"[PhysicsExpert] 🔄 Expert asked for clarification. Sending FORCE_OPTIONS_PROMPT...")
            self.messages.append({"role": "user", "content": FORCE_OPTIONS_PROMPT})

            response = call_model(self.messages, use_web=use_web)
            print(f"[PhysicsExpert] ✅ Received forced response from OpenAI.")
            self.messages.append({"role": "assistant", "content": response})

        # Save to file if configured
        if self.use_file_history:
            save_chat_history(self.messages)

        return response
