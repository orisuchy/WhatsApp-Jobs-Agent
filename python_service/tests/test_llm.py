import json

import httpx
import openai
import pytest

from llm import OllamaLLM, OpenAILLM, complete_json, load_llm_client
from main import JobMatchingResult

GOOD = {"odds": 80, "reason": "r", "company": "Acme", "summary": "s"}


class FakeLLM:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), 0

    def complete(self, messages, schema=None, keep_alive=None):
        self.calls += 1
        return self.replies.pop(0)


def _run(*replies):
    c = FakeLLM(*replies)
    return complete_json(c, [], JobMatchingResult), c.calls


def test_valid_reply():
    assert _run(json.dumps(GOOD)) == (GOOD, 1)


def test_fenced_reply():
    assert _run("Sure!\n```json\n" + json.dumps(GOOD) + "\n```")[0] == GOOD


def test_invalid_then_valid_retries_once():
    assert _run("nope", json.dumps(GOOD)) == (GOOD, 2)


def test_schema_invalid_then_valid():
    assert _run(json.dumps({"odds": 1}), json.dumps(GOOD)) == (GOOD, 2)


def test_invalid_twice_is_no_match():
    assert _run("nope", "still nope") == (None, 2)


def test_provider_selection_defaults_to_ollama():
    c = load_llm_client({})
    assert isinstance(c, OllamaLLM) and c.num_ctx == 16384


def test_ollama_legacy_env_fallback():
    c = load_llm_client({"OLLAMA_MODEL": "m1", "OLLAMA_URL": "http://x:1"})
    assert c.model == "m1"


def test_openai_selected():
    c = load_llm_client({"LLM_PROVIDER": "openai", "LLM_MODEL": "gpt-x", "LLM_API_KEY": "k"})
    assert isinstance(c, OpenAILLM) and c.model == "gpt-x"


def test_unknown_provider_rejected():
    with pytest.raises(ValueError):
        load_llm_client({"LLM_PROVIDER": "bogus"})


def test_openai_falls_back_when_json_schema_rejected():
    seen = []

    class Completions:
        msg, always = "Unsupported response_format json_schema", False

        def create(self, **kw):
            seen.append(kw.get("response_format", {}).get("type"))
            if len(seen) == 1 or self.always:
                req = httpx.Request("POST", "http://x")
                raise openai.BadRequestError(self.msg, response=httpx.Response(400, request=req), body=None)
            msg = type("M", (), {"content": "{}"})
            return type("R", (), {"choices": [type("C", (), {"message": msg})]})

    c = OpenAILLM("http://x/v1", "k", "m", 5)
    c._client = type("Cl", (), {"chat": type("Ch", (), {"completions": Completions()})})
    assert c.complete([], schema={"type": "object"}) == "{}"
    assert seen == ["json_schema", "json_object"]


def test_openai_bad_model_400_is_not_retried():
    seen = []

    class Completions:
        def create(self, **kw):
            seen.append(1)
            req = httpx.Request("POST", "http://x")
            raise openai.BadRequestError("model not found", response=httpx.Response(400, request=req), body=None)

    c = OpenAILLM("http://x/v1", "k", "m", 5)
    c._client = type("Cl", (), {"chat": type("Ch", (), {"completions": Completions()})})
    with pytest.raises(openai.BadRequestError):
        c.complete([], schema={"type": "object"})
    assert len(seen) == 1
