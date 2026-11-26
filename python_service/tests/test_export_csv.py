"""Exporting matched jobs as CSV (GET /api/jobs.csv): opens in Excel and Sheets, Hebrew included."""
import csv
import io

from fastapi.testclient import TestClient

import main
import outputs


def _setup(monkeypatch, tmp_path, texts=("Python dev at Acme",)):
    monkeypatch.setattr(main, "DATA_DIR", str(tmp_path))
    for i, t in enumerate(texts):
        assert outputs.record_if_new(str(tmp_path), {
            "company": f"C{i}", "text": t, "score": 80, "summary": "Backend role", "reason": "Good fit",
            "group": "Python", "url": "https://example.com/job", "timestamp": "2026-11-20T10:00:00+00:00",
        })


def _csv(client):
    r = client.get("/api/jobs.csv")
    assert r.status_code == 200
    return r, r.content.decode("utf-8-sig")  # decoded without the BOM, as Excel reads it


def test_export_has_header_and_one_row_with_note_and_status(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    with TestClient(main.app) as c:
        jid = c.get("/api/jobs").json()[0]["id"]
        c.patch(f"/api/jobs/{jid}", json={"note": "Call Dana", "status": "applied"})
        r, text = _csv(c)
    assert r.headers["content-disposition"] == 'attachment; filename="job-matches.csv"'
    assert r.headers["content-type"].startswith("text/csv")
    assert r.content.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM so Excel reads Hebrew correctly
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == 1
    row = rows[0]
    assert row["company"] == "C0" and row["score"] == "80" and row["URL"] == "https://example.com/job"
    assert row["note"] == "Call Dana" and row["status"] == "Applied"
    assert row["starred"] == "" and row["hidden"] == "" and row["reminder"] == ""


def test_cells_starting_with_formula_characters_are_neutralised(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, texts=("=SUM(A1:A2) posting",))
    with TestClient(main.app) as c:
        jid = c.get("/api/jobs").json()[0]["id"]
        c.patch(f"/api/jobs/{jid}", json={"note": "+1 call, -later @Dana"})
        _, text = _csv(c)
    row = next(csv.DictReader(io.StringIO(text)))
    assert row["original text"] == "'=SUM(A1:A2) posting"  # Excel would otherwise run it as a formula
    assert row["note"] == "'+1 call, -later @Dana"
    assert row["score"] == "80" and row["company"] == "C0"  # ordinary cells untouched


def test_empty_store_gives_only_the_header(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "DATA_DIR", str(tmp_path))
    with TestClient(main.app) as c:
        r, text = _csv(c)
    assert r.status_code == 200
    assert text.splitlines() == [",".join(outputs.CSV_HEADER)]


def test_starred_and_hidden_jobs_export_as_yes(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    with TestClient(main.app) as c:
        jid = c.get("/api/jobs").json()[0]["id"]
        c.patch(f"/api/jobs/{jid}", json={"starred": True, "hidden": True})
        _, text = _csv(c)
    row = next(csv.DictReader(io.StringIO(text)))
    assert row["starred"] == "yes" and row["hidden"] == "yes"
