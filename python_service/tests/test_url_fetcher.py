import pytest

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


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "http://127.0.0.1/admin",
        "http://localhost:8000/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
    ],
)
def test_fetch_url_text_refuses_unsafe_urls(url, monkeypatch):
    def _no_network(*a, **k):
        raise AssertionError("must not fetch")

    monkeypatch.setattr(main.requests, "get", _no_network)
    with pytest.raises(ValueError):
        main.fetch_url_text(url)


def test_extract_fetched_data_treats_refused_url_as_failed_fetch():
    assert extract_fetched_data("דרוש http://127.0.0.1/secret") == ("no fetched data", None)


class _Resp:
    def __init__(self, location=None, text="<p>job</p>"):
        self.headers = {"Location": location} if location else {}
        self.is_redirect = bool(location)
        self.text = text

    def raise_for_status(self):
        pass


def _stub_hosts(monkeypatch, pages):
    monkeypatch.setattr(main, "assert_safe_url", lambda u: (_ for _ in ()).throw(ValueError(u)) if "internal" in u else None)
    monkeypatch.setattr(main.requests, "get", lambda u, **k: pages[u])


def test_fetch_follows_redirect_chain(monkeypatch):
    _stub_hosts(monkeypatch, {"http://s.co/a": _Resp("https://example.com/job"), "https://example.com/job": _Resp()})
    assert main.fetch_url_text("http://s.co/a")[0] == "job"


def test_fetch_refuses_redirect_to_private_hop(monkeypatch):
    _stub_hosts(monkeypatch, {"http://s.co/a": _Resp("http://internal/x")})
    with pytest.raises(ValueError):
        main.fetch_url_text("http://s.co/a")


def test_fetch_stops_after_five_redirects(monkeypatch):
    calls = []

    def get(u, **k):
        calls.append(u)
        return _Resp("http://s.co/loop")

    monkeypatch.setattr(main, "assert_safe_url", lambda u: None)
    monkeypatch.setattr(main.requests, "get", get)
    with pytest.raises(ValueError):
        main.fetch_url_text("http://s.co/loop")
    assert len(calls) == 6
