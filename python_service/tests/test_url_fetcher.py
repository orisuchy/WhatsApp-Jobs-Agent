import main
from main import extract_first_url, build_posting_text


def test_extract_first_url_finds_url_in_text():
    text = "דרוש מפתח Python! פרטים כאן: https://example.com/jobs/123 מייד"
    assert extract_first_url(text) == "https://example.com/jobs/123"


def test_extract_first_url_returns_none_when_absent():
    assert extract_first_url("דרוש מפתח Python, פנו בהודעה פרטית") is None


def test_build_posting_text_passes_through_when_no_url():
    assert build_posting_text("דרוש מפתח Python") == "דרוש מפתח Python"


def test_build_posting_text_concatenates_fetched_content(monkeypatch):
    monkeypatch.setattr(
        main, "fetch_url_text", lambda url, timeout=10: "Full job description here"
    )
    result = build_posting_text("דרוש מפתח Python https://example.com/jobs/123")
    assert "דרוש מפתח Python" in result
    assert "Full job description here" in result
    assert result.index("דרוש מפתח Python") < result.index("Full job description here")


def test_build_posting_text_falls_back_when_fetch_fails(monkeypatch):
    def _raise(url, timeout=10):
        raise RuntimeError("network down")

    monkeypatch.setattr(main, "fetch_url_text", _raise)
    result = build_posting_text("דרוש מפתח Python https://example.com/jobs/123")
    assert result == "דרוש מפתח Python https://example.com/jobs/123"
