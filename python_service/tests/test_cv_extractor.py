import os

import pytest

from main import extract_cv_text

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
SAMPLE_CV = os.path.join(FIXTURES_DIR, "sample_cv.pdf")


def test_extract_cv_text_returns_known_content():
    text = extract_cv_text(SAMPLE_CV)
    assert "Python" in text
    assert "FastAPI" in text


def test_extract_cv_text_missing_file_raises():
    with pytest.raises(Exception):
        extract_cv_text(os.path.join(FIXTURES_DIR, "does_not_exist.pdf"))
