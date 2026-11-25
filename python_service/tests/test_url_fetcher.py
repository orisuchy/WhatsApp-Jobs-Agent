import main
from main import extract_first_url, extract_fetched_data


def test_extract_first_url_finds_url_in_text():
    text = "דרוש מפתח Python! פרטים כאן: https://example.com/jobs/123 מייד"
    assert extract_first_url(text) == "https://example.com/jobs/123"


def test_extract_first_url_returns_none_when_absent():
    assert extract_first_url("דרוש מפתח Python, פנו בהודעה פרטית") is None


def test_extract_fetched_data_no_url():
    assert extract_fetched_data("דרוש מפתח Python") == ("no fetched data", None)


def test_extract_fetched_data_returns_fetched_text_and_company(monkeypatch):
    monkeypatch.setattr(
        main,
        "fetch_url_text",
        lambda url, timeout=30: ("Full job description here", "Acme"),
    )
    result = extract_fetched_data("דרוש מפתח Python https://example.com/jobs/123")
    assert result == ("Full job description here", "Acme")


def test_extract_fetched_data_falls_back_when_fetch_fails(monkeypatch):
    def _raise(url, timeout=30):
        raise RuntimeError("network down")

    monkeypatch.setattr(main, "fetch_url_text", _raise)
    result = extract_fetched_data("דרוש מפתח Python https://example.com/jobs/123")
    assert result == ("no fetched data", None)
