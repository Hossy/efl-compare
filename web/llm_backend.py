"""
OpenAI-standard LLM backend for the EFL comparator.

All LLM access goes through the OpenAI Chat Completions API (the `openai`
package), so the tool works against ANY OpenAI-compatible server — a local
llama.cpp server, vLLM, Ollama, LM Studio, or the hosted OpenAI API — without
code changes. Pick your model by editing `.env` (copy `.env.example`):

    EFL_LLM_BASE_URL          base URL, e.g. http://127.0.0.1:8080/v1
    EFL_LLM_MODEL             model id to request, e.g. qwen3.6-35b
    EFL_LLM_API_KEY           API key (any placeholder for keyless local servers)
    EFL_LLM_TIMEOUT           per-request timeout in seconds (default 120)
    EFL_LLM_DISABLE_THINKING  "true" to suppress reasoning traces on reasoning
                              models (Qwen3, etc.) so token budgets aren't spent
                              thinking. Sent as chat_template_kwargs.enable_thinking
                              = false via the OpenAI `extra_body` escape hatch.

Structured output uses the OpenAI `response_format` = `json_schema` standard, so
the model is constrained to the exact JSON schema each caller supplies.
"""

from __future__ import annotations

import os
from pathlib import Path


class LLMNotConfigured(RuntimeError):
    """Raised when no LLM endpoint is configured (EFL_LLM_BASE_URL unset)."""


# ---------------------------------------------------------------------------
# .env loading (no third-party dependency)
# ---------------------------------------------------------------------------
_dotenv_loaded = False


def load_dotenv(path: "str | Path | None" = None, override: bool = False) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ.

    Real environment variables win over .env by default (override=False), so a
    contributor's shell/session settings are never silently clobbered. Called
    once, early, by efl_compare. Safe to call repeatedly.
    """
    global _dotenv_loaded
    if path is None:
        path = Path(__file__).resolve().parent / ".env"
    path = Path(path)
    if not path.is_file():
        _dotenv_loaded = True
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = val
    _dotenv_loaded = True


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def _bool_env(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def is_configured() -> bool:
    """True if an LLM endpoint has been configured."""
    return bool(os.environ.get("EFL_LLM_BASE_URL", "").strip())


def config() -> dict:
    base = os.environ.get("EFL_LLM_BASE_URL", "").strip()
    if not base:
        raise LLMNotConfigured(
            "No LLM endpoint configured. Set EFL_LLM_BASE_URL (and EFL_LLM_MODEL) "
            "in your .env file — copy .env.example to .env and edit it. It can point "
            "at any OpenAI-compatible server (llama.cpp server, vLLM, Ollama, LM "
            "Studio, or the OpenAI API). To run without an LLM, pass --no-llm."
        )
    return {
        "base_url":         base,
        "model":            os.environ.get("EFL_LLM_MODEL", "").strip() or "default",
        "api_key":          os.environ.get("EFL_LLM_API_KEY", "").strip() or "sk-no-key-required",
        "timeout":          float(os.environ.get("EFL_LLM_TIMEOUT", "120") or 120),
        "disable_thinking": _bool_env("EFL_LLM_DISABLE_THINKING", False),
    }


def label() -> str:
    """Human-readable 'model @ host' for status/stat lines."""
    try:
        c = config()
        from urllib.parse import urlparse
        host = urlparse(c["base_url"]).netloc or c["base_url"]
        return f"{c['model']} @ {host}"
    except Exception:
        return "unconfigured"


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------
class ChatBackend:
    """Thin adapter over the OpenAI Chat Completions API.

    Exposes create_chat_completion() with a llama-cpp-python-shaped return value
    (choices[0].message.content + usage) so existing call sites change minimally.
    JSON-schema-constrained output is requested via the OpenAI response_format
    standard; reasoning is optionally disabled via extra_body.
    """

    def __init__(self):
        self.cfg = config()
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                "The 'openai' package is required for LLM parsing. Install deps:\n"
                "    python3 -m pip install -r requirements.txt\n"
                "(or run with --no-llm to skip all LLM calls)."
            ) from exc
        self.client = OpenAI(
            base_url=self.cfg["base_url"],
            api_key=self.cfg["api_key"],
            timeout=self.cfg["timeout"],
        )

    def create_chat_completion(self, messages, temperature: float = 0.0,
                               max_tokens: int = 256, response_schema=None,
                               schema_name: str = "output") -> dict:
        kwargs = dict(
            model=self.cfg["model"],
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if response_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": response_schema},
            }
        extra_body = {}
        if self.cfg["disable_thinking"]:
            # Not part of the OpenAI schema; passed through to llama.cpp / Qwen
            # via the SDK's extra_body escape hatch.
            extra_body["chat_template_kwargs"] = {"enable_thinking": False}
        if extra_body:
            kwargs["extra_body"] = extra_body

        resp = self.client.chat.completions.create(**kwargs)
        usage = getattr(resp, "usage", None)
        return {
            "choices": [
                {"message": {"content": resp.choices[0].message.content or ""}}
            ],
            "usage": {
                "prompt_tokens":     getattr(usage, "prompt_tokens", 0) if usage else 0,
                "completion_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
            },
        }
