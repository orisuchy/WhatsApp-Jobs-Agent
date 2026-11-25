"""LLM backends behind one tiny interface: client.complete(messages, schema=None, keep_alive=None) -> str."""
import json
import os

from pydantic import ValidationError

from logger import setup_logger

logger = setup_logger("job-agent.llm")

DEFAULT_MODEL = "gemma4:e2b"  # keep in sync with .env.example and README
DEFAULT_TIMEOUT_S = 170
DEFAULT_OLLAMA_URL = "http://host.docker.internal:11434"


class OllamaLLM:
    def __init__(self, base_url: str, model: str, num_ctx: int, timeout: float, think: bool = False):
        from ollama import Client

        self.model, self.num_ctx, self.think = model, num_ctx, think
        self._client = Client(host=base_url, timeout=timeout)

    def complete(self, messages, schema=None, keep_alive=None) -> str:
        response = self._client.chat(
            model=self.model,
            messages=messages,
            format=schema,
            options={"temperature": 0.0, "num_ctx": self.num_ctx},
            keep_alive=keep_alive,
            think=self.think,  # thinking models (gemma4) otherwise reason for minutes before the JSON
        )
        return response["message"]["content"]


class OpenAILLM:
    def __init__(self, base_url: str | None, api_key: str, model: str, timeout: float):
        from openai import OpenAI

        self.model = model
        # SDK rejects an empty key; local servers (LM Studio) ignore it.
        self._client = OpenAI(base_url=base_url or None, api_key=api_key or "none", timeout=timeout, max_retries=0)

    def complete(self, messages, schema=None, keep_alive=None) -> str:
        from openai import APIStatusError

        formats = [None]
        if schema:
            formats = [
                {"type": "json_schema", "json_schema": {"name": "result", "schema": schema}},
                {"type": "json_object"},
                None,
            ]
        for i, fmt in enumerate(formats):
            kwargs = {"response_format": fmt} if fmt else {}
            msgs = messages
            if schema and (fmt is None or fmt["type"] != "json_schema"):
                # schema not enforced by the provider: describe it compactly (in the first system message)
                note = "\n\nReply with only a JSON object matching this JSON schema: " + json.dumps(schema, separators=(",", ":"))
                if messages and messages[0]["role"] == "system":
                    msgs = [{**messages[0], "content": messages[0]["content"] + note}, *messages[1:]]
                else:
                    msgs = [{"role": "system", "content": note.strip()}, *messages]
            try:
                r = self._client.chat.completions.create(
                    model=self.model, messages=msgs, temperature=0.0, **kwargs
                )
                return r.choices[0].message.content or ""
            except APIStatusError as e:
                # provider rejected the format parameter: try the next, weaker one
                about_format = "response_format" in str(e).lower() or "json_schema" in str(e).lower() or "json_object" in str(e).lower()
                if e.status_code not in (400, 422) or not about_format or i == len(formats) - 1:
                    raise
                logger.warning("Provider rejected response_format %s, falling back", fmt and fmt["type"])


def load_llm_client(env=os.environ):
    provider = env.get("LLM_PROVIDER", "ollama").strip().lower()
    timeout = float(env.get("LLM_TIMEOUT_S", DEFAULT_TIMEOUT_S))
    if provider == "ollama":
        return OllamaLLM(
            env.get("LLM_BASE_URL") or env.get("OLLAMA_URL") or DEFAULT_OLLAMA_URL,
            env.get("LLM_MODEL") or env.get("OLLAMA_MODEL") or DEFAULT_MODEL,
            int(env.get("LLM_NUM_CTX", 16384)),
            timeout,
            env.get("LLM_THINK", "").strip().lower() in ("1", "true", "yes", "on"),
        )
    if provider == "openai":
        if not env.get("LLM_MODEL"):
            raise ValueError("LLM_MODEL is required when LLM_PROVIDER=openai")
        return OpenAILLM(env.get("LLM_BASE_URL"), env.get("LLM_API_KEY", ""), env["LLM_MODEL"], timeout)
    raise ValueError(f"Unknown LLM_PROVIDER {provider!r} (use 'ollama' or 'openai')")


def is_timeout(e: BaseException) -> bool:
    import httpx
    from openai import APITimeoutError

    return isinstance(e, (httpx.TimeoutException, APITimeoutError))


def log_if_timeout(e: BaseException, env=os.environ) -> bool:
    """Log the plain timeout hint and return True when e is an LLM timeout."""
    if not is_timeout(e):
        return False
    logger.error("LLM timed out after %s s — raise LLM_TIMEOUT_S or use a smaller model", env.get("LLM_TIMEOUT_S", DEFAULT_TIMEOUT_S))
    return True


def parse_json_object(text: str) -> dict:
    """First JSON object in text; tolerates code fences and surrounding chatter."""
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(obj, dict):
        raise ValueError("reply is not a JSON object")
    return obj


def complete_json(client, messages, model_cls, keep_alive=None, override=None):
    """Ask for a model_cls-shaped dict; one retry on a malformed/invalid reply, else None (no-match).
    `override` fields are forced onto the parsed reply before validation."""
    for attempt in (1, 2):
        text = client.complete(messages, schema=model_cls.model_json_schema(), keep_alive=keep_alive)
        try:
            return model_cls(**{**parse_json_object(text), **(override or {})}).model_dump()
        except (ValueError, ValidationError):
            logger.warning("Malformed LLM reply (attempt %d): %.200s", attempt, text)
    return None


# --- Model metadata (for the UI's model list and context-window check) ------
CONTEXT_KEYS = ("context_length", "context_window", "max_context_length", "max_model_len", "max_input_tokens")


def _provider_base(env):
    provider = env.get("LLM_PROVIDER", "ollama").strip().lower()
    if provider == "ollama":
        return provider, (env.get("LLM_BASE_URL") or env.get("OLLAMA_URL") or DEFAULT_OLLAMA_URL).rstrip("/")
    return provider, (env.get("LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/")


def _openai_models(base, env) -> list[dict]:
    import requests

    r = requests.get(f"{base}/models", headers={"Authorization": f"Bearer {env.get('LLM_API_KEY') or 'none'}"}, timeout=5)
    r.raise_for_status()
    return r.json().get("data", [])


def list_models(env=os.environ) -> tuple[list[str], str | None]:
    """Model names the provider offers, or ([], error message)."""
    import requests

    provider, base = _provider_base(env)
    try:
        if provider == "ollama":
            r = requests.get(f"{base}/api/tags", timeout=5)
            r.raise_for_status()
            return sorted(m["name"] for m in r.json().get("models", [])), None
        return sorted(m.get("id", "") for m in _openai_models(base, env) if m.get("id")), None
    except Exception as e:
        return [], f"Couldn't list models from {base}: {e}"


def model_context(env=os.environ) -> tuple[int | None, str | None]:
    """The model's maximum context window in tokens, or (None, error message / None if the provider doesn't say)."""
    import requests

    provider, base = _provider_base(env)
    model = env.get("LLM_MODEL") or (env.get("OLLAMA_MODEL") if provider == "ollama" else "") or (DEFAULT_MODEL if provider == "ollama" else "")
    try:
        if provider == "ollama":
            r = requests.post(f"{base}/api/show", json={"model": model}, timeout=5)
            if r.status_code == 404:
                return None, f"Model {model!r} isn't installed in Ollama. Run: ollama pull {model}"
            r.raise_for_status()
            info = r.json().get("model_info", {})
            return next((int(v) for k, v in info.items() if k.endswith(".context_length")), None), None
        for m in _openai_models(base, env):
            if m.get("id") == model:
                found = [m.get(k) for k in CONTEXT_KEYS] + [(m.get("top_provider") or {}).get("context_length")]
                return next((int(v) for v in found if v), None), None
        return None, None  # provider doesn't list it (aliases are common): can't tell
    except Exception as e:
        return None, f"Couldn't reach {base}: {e}"
