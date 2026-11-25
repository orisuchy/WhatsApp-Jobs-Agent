"""LLM backends behind one tiny interface: client.complete(messages, schema=None, keep_alive=None) -> str."""
import json
import os

from pydantic import ValidationError

from logger import setup_logger

logger = setup_logger("job-agent.llm")

DEFAULT_MODEL = "gemma4:e2b"  # keep in sync with .env.example and README
DEFAULT_OLLAMA_URL = "http://host.docker.internal:11434"


class OllamaLLM:
    def __init__(self, base_url: str, model: str, num_ctx: int, timeout: float):
        from ollama import Client

        self.model, self.num_ctx = model, num_ctx
        self._client = Client(host=base_url, timeout=timeout)

    def complete(self, messages, schema=None, keep_alive=None) -> str:
        response = self._client.chat(
            model=self.model,
            messages=messages,
            format=schema,
            options={"temperature": 0.0, "num_ctx": self.num_ctx},
            keep_alive=keep_alive,
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
                # schema not enforced by the provider: describe it compactly
                msgs = messages + [{"role": "system", "content": "Reply with only a JSON object matching this JSON schema: " + json.dumps(schema, separators=(",", ":"))}]
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
    timeout = float(env.get("LLM_TIMEOUT_S", 170))
    if provider == "ollama":
        return OllamaLLM(
            env.get("LLM_BASE_URL") or env.get("OLLAMA_URL") or DEFAULT_OLLAMA_URL,
            env.get("LLM_MODEL") or env.get("OLLAMA_MODEL") or DEFAULT_MODEL,
            int(env.get("LLM_NUM_CTX", 16384)),
            timeout,
        )
    if provider == "openai":
        if not env.get("LLM_MODEL"):
            raise ValueError("LLM_MODEL is required when LLM_PROVIDER=openai")
        return OpenAILLM(env.get("LLM_BASE_URL"), env.get("LLM_API_KEY", ""), env["LLM_MODEL"], timeout)
    raise ValueError(f"Unknown LLM_PROVIDER {provider!r} (use 'ollama' or 'openai')")


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
