"""
LLM provider for DIA Agentic.

Priority order (controlled via .env):
  1. CLOUDFLARE_AUTH_TOKEN + CLOUDFLARE_ACCOUNT_ID  →  Cloudflare Workers AI
                                                        Fast, generous free tier, no card needed
  2. GROQ_API_KEY                                   →  Groq cloud
                                                        Also fast and free; needs api.groq.com reachable
  3. FOUNDRY_MODEL=<alias>                          →  Foundry Local on-device (slow, ~15s/call)
                                                        No internet required; last resort

Cloudflare models used:
  - Intent / chat:    @cf/meta/llama-3.3-70b-instruct-fp8-fast  (~0.7s, ~95 neurons/call)
  - Code / tests:     @cf/qwen/qwen2.5-coder-32b-instruct       (~0.9s, higher neuron cost)
  - Both via the OpenAI-compatible endpoint at:
    https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1
"""
import json
import os
import subprocess
import threading

# ── Cloudflare model names ────────────────────────────────────────────────────
CF_MODEL_CHAT    = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"   # intent parsing — clean JSON, ~0.7s, ~41 neurons/call
CF_MODEL_CODE    = "@cf/qwen/qwen2.5-coder-32b-instruct"        # code-optimised — test generation
CF_MODEL_SYNTH   = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"  # synthesis — vendor/test explanations
# qwen3-30b-a3b-fp8: uses thinking mode → thinking tokens exhaust 600-token budget before JSON → parse errors
# Switch back with: CF_MODEL_CHAT = "@cf/qwen/qwen3-30b-a3b-fp8" + increase max_tokens to 4000

# ── Foundry Local subprocess client (kept commented; restore if needed) ──────
#
# class _Message:
#     def __init__(self, content): self.content = content
# class _Choice:
#     def __init__(self, content): self.message = _Message(content)
# class _CompletionResponse:
#     def __init__(self, content, model):
#         self.choices = [_Choice(content)]; self.model = model
# class _Completions:
#     def __init__(self, alias): self._alias = alias
#     def create(self, model=None, messages=None, temperature=0.0, max_tokens=500, **_kw):
#         messages = messages or []
#         system = next((m["content"] for m in messages if m.get("role") == "system"), "")
#         turns  = [m for m in messages if m.get("role") in ("user", "assistant")]
#         parts  = []
#         if system:
#             sys_text = system[:4000] + ("..." if len(system) > 4000 else "")
#             parts.append(f"[SYSTEM]\n{sys_text}\n[/SYSTEM]\n")
#         for m in turns:
#             label = "User" if m.get("role") == "user" else "Assistant"
#             parts.append(f"\n{label}: {m.get('content', '')}")
#         prompt = "".join(parts).strip()
#         if len(prompt) > 6000:
#             last_user = next((m.get("content","") for m in reversed(turns) if m.get("role")=="user"), "")
#             sys_text  = system[:2500] + ("..." if len(system) > 2500 else "")
#             prompt = f"[SYSTEM]\n{sys_text}\n[/SYSTEM]\n\nUser: {last_user}"
#         effective_tokens = max(int(max_tokens), 1500)
#         try:
#             result = subprocess.run(
#                 ["foundry", "complete", self._alias, prompt, "-o", "json", "-m", str(effective_tokens)],
#                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
#             )
#             raw = (result.stdout or "").strip() or (result.stderr or "").strip()
#             if raw:
#                 data = json.loads(raw)
#                 if "error" in data:
#                     return _CompletionResponse(f"[foundry-error] {data['error'].get('message','?')}", self._alias)
#                 return _CompletionResponse(data.get("text","").strip(), self._alias)
#         except subprocess.TimeoutExpired:
#             return _CompletionResponse("[foundry-error] timed out", self._alias)
#         except Exception as e:
#             return _CompletionResponse(f"[foundry-error] {e}", self._alias)
#         return _CompletionResponse("", self._alias)
# class _Chat:
#     def __init__(self, alias): self.completions = _Completions(alias)
# class FoundryCLIClient:
#     def __init__(self, alias): self.chat = _Chat(alias)
# _load_lock   = threading.Lock()
# _loaded_alias: str | None = None
# def _ensure_model_loaded(alias):
#     global _loaded_alias
#     with _load_lock:
#         if _loaded_alias == alias: return
#         try:
#             subprocess.run(["foundry","model","load",alias], capture_output=True, text=True, timeout=60)
#             _loaded_alias = alias
#         except Exception: pass


# ── Public API ────────────────────────────────────────────────────────────────

def get_client() -> tuple:
    """
    Returns (openai.OpenAI client, model_id string).

    Provider priority: Cloudflare → Groq → Foundry Local.
    All return an OpenAI-SDK-compatible client.
    """
    from openai import OpenAI  # type: ignore

    # ── 1. Cloudflare Workers AI ─────────────────────────────────────────────
    cf_token   = os.getenv("CLOUDFLARE_AUTH_TOKEN", "").strip()
    cf_account = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
    if cf_token and cf_account:
        client = OpenAI(
            base_url=f"https://api.cloudflare.com/client/v4/accounts/{cf_account}/ai/v1",
            api_key=cf_token,
        )
        return client, CF_MODEL_CHAT

    # ── 2. Groq ──────────────────────────────────────────────────────────────
    groq_key = next(
        (os.getenv(k, "").strip() for k in ("GROQ_API_KEY", "GROQ_API_KEY_2")
         if os.getenv(k, "").strip()),
        None,
    )
    if groq_key:
        return (
            OpenAI(base_url="https://api.groq.com/openai/v1", api_key=groq_key),
            "qwen/qwen3.8-27b",
        )

    # ── 3. Foundry Local (commented — uncomment FoundryCLIClient above first)
    # foundry_alias = os.getenv("FOUNDRY_MODEL", "").strip()
    # if foundry_alias:
    #     _ensure_model_loaded(foundry_alias)
    #     return FoundryCLIClient(foundry_alias), foundry_alias

    raise RuntimeError(
        "No LLM provider configured in .env.\n"
        "  Option A — Cloudflare (fast, free, no card):\n"
        "             CLOUDFLARE_AUTH_TOKEN=<token>\n"
        "             CLOUDFLARE_ACCOUNT_ID=<account_id>\n"
        "  Option B — Groq (fast, free, no card):\n"
        "             GROQ_API_KEY=<key>\n"
        "  Option C — Foundry Local (no internet, slow):\n"
        "             Uncomment Foundry section in llm_provider.py\n"
        "             FOUNDRY_MODEL=qwen3-1.7b"
    )


def get_synthesis_client() -> tuple:
    """
    Returns the smartest available client for narrative synthesis
    (explain_vendor, explain_test, rich summaries).
    Uses llama-3.3-70b on Cloudflare; falls back to get_client().
    """
    cf_token   = os.getenv("CLOUDFLARE_AUTH_TOKEN", "").strip()
    cf_account = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
    if cf_token and cf_account:
        from openai import OpenAI  # type: ignore
        client = OpenAI(
            base_url=f"https://api.cloudflare.com/client/v4/accounts/{cf_account}/ai/v1",
            api_key=cf_token,
        )
        return client, CF_MODEL_SYNTH
    return get_client()


def get_cloudflare_code_client() -> tuple:
    """
    Returns a Cloudflare client using the code-optimised model.
    Falls back to get_client() if Cloudflare is not configured.
    Used by test_builder.py for Python test generation.
    """
    cf_token   = os.getenv("CLOUDFLARE_AUTH_TOKEN", "").strip()
    cf_account = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
    if cf_token and cf_account:
        from openai import OpenAI  # type: ignore
        client = OpenAI(
            base_url=f"https://api.cloudflare.com/client/v4/accounts/{cf_account}/ai/v1",
            api_key=cf_token,
        )
        return client, CF_MODEL_CODE
    return get_client()


def get_groq_keys() -> list[str]:
    """Returns configured Groq API keys (for callers that do key rotation)."""
    return [
        v for k in ("GROQ_API_KEY", "GROQ_API_KEY_2")
        if (v := os.getenv(k, "").strip())
    ]


def is_foundry_active() -> bool:
    """True when Foundry Local is the active provider."""
    return False   # Foundry is commented out; Cloudflare/Groq are active
