import json
from unittest.mock import MagicMock

from main import analyze_job_posting, JobMatchingResult


def _mock_client(content: str) -> MagicMock:
    client = MagicMock()
    client.chat.return_value = {"message": {"content": content}}
    return client


def test_analyze_job_posting_returns_match():
    payload = json.dumps(
        {
            "odds": "90%",
            "reason": "התאמה גבוהה בין הדרישות לניסיון",
            "company": "Acme",
            "summary": "מפתח/ת Python ב-Acme, לפניה יש לשלוח קו״ח",
        }
    )
    client = _mock_client(payload)
    result = analyze_job_posting(
        "Python developer at Acme", "no fetched data", "Python, FastAPI experience", client, "llama3.1"
    )
    assert result["odds"] == "90%"
    assert result["company"] == "Acme"
    assert "reason" in result
    assert "summary" in result


def test_analyze_job_posting_returns_no_match():
    payload = json.dumps(
        {"odds": "5%", "reason": "אין התאמה", "company": "N/A", "summary": ""}
    )
    client = _mock_client(payload)
    result = analyze_job_posting(
        "Chef needed", "no fetched data", "Python, FastAPI experience", client, "llama3.1"
    )
    assert result["odds"] == "5%"


def test_analyze_job_posting_malformed_json_falls_back():
    client = _mock_client("not valid json")
    result = analyze_job_posting(
        "Python developer", "no fetched data", "Python experience", client, "llama3.1"
    )
    assert result == {"odds": 0}


def test_analyze_job_posting_client_raises_falls_back():
    client = MagicMock()
    client.chat.side_effect = RuntimeError("ollama unreachable")
    result = analyze_job_posting(
        "Python developer", "no fetched data", "Python experience", client, "llama3.1"
    )
    assert result == {"odds": 0}


def test_analyze_job_posting_calls_chat_with_json_format():
    payload = json.dumps(
        {"odds": "5%", "reason": "אין התאמה", "company": "N/A", "summary": ""}
    )
    client = _mock_client(payload)
    analyze_job_posting("posting", "no fetched data", "cv text", client, "llama3.1")
    _, kwargs = client.chat.call_args
    assert kwargs["format"] == JobMatchingResult.model_json_schema()
    assert kwargs["model"] == "llama3.1"


def test_analyze_job_posting_uses_preextracted_company():
    payload = json.dumps(
        {"odds": "85%", "reason": "התאמה טובה", "company": "WrongCompany", "summary": "תיאור"}
    )
    client = _mock_client(payload)
    result = analyze_job_posting(
        "posting", "no fetched data", "cv text", client, "llama3.1", company_name="CodeValue"
    )
    assert result["company"] == "CodeValue"
    assert "IMPORTANT: The hiring company name is: CodeValue" in client.chat.call_args[1]["messages"][1]["content"]
