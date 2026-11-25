import json
import os
import re
import ipaddress
import socket
from contextlib import asynccontextmanager
from threading import Lock
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from fastapi import FastAPI
from ollama import Client
from pydantic import BaseModel, Field
from pypdf import PdfReader
import requests

from prompts import JOB_MATCHING_ASSISTANT, CV_ORGANIZER
from logger import setup_logger

logger = setup_logger("job-agent")


class JobMatchingResult(BaseModel):
    odds: str = Field(..., description="Percentage match, e.g., '80%'")
    reason: str = Field(..., description="One-sentence explanation in Hebrew")
    company: str = Field(..., description="Name of the hiring company")
    summary: str = Field(..., description="1-2 sentence summary in Hebrew")


def extract_cv_text(path: str) -> str:
    reader = PdfReader(path)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


URL_PATTERN = re.compile(r"https?://\S+")


def extract_first_url(text: str):
    match = URL_PATTERN.search(text)
    return match.group(0) if match else None


def assert_safe_url(url: str) -> None:
    """Refuse non-http(s) URLs and hosts resolving to non-public addresses."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"Unsafe URL scheme: {url}")
    # ponytail: resolve-then-fetch leaves a DNS-rebinding window; pin the IP if that matters
    for info in socket.getaddrinfo(parsed.hostname, None):
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise ValueError(f"Refusing non-public address {ip} for {url}")

def fetch_url_text(url: str, timeout: int = 30) -> tuple[str, str | None]:
    # Standard HTTP fetch with BeautifulSoup
    try:
        assert_safe_url(url)
        response = requests.get(url, timeout=timeout, allow_redirects=False, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        # Extract company name if this is a hiremetech.com URL
        company_name = None
        if "hiremetech.com" in url:
            company_link = soup.select_one("div.flex.items-center.justify-center.gap-2.mb-2 > a")
            if company_link:
                company_name = company_link.get_text(strip=True)

        # Extract text content
        for tag in soup(["script", "style"]):
            tag.decompose()
        lines = [
            line.strip()
            for line in soup.get_text(separator="\n").splitlines()
            if line.strip()
        ]
        text = "\n".join(lines)[:8000]
        return text, company_name
    except Exception as e:
        logger.debug("Failed to fetch %s: %s", url, e)
        raise


def extract_fetched_data(original_text: str) -> tuple[str, str | None]:
    """Extract fetched data and pre-extracted company name for hiremetech.com URLs."""
    url = extract_first_url(original_text)
    if not url:
        return "no fetched data", None

    try:
        fetched, company_name = fetch_url_text(url)
    except Exception:
        logger.exception("Failed to fetch job posting URL: %s", url)
        return "no fetched data", None
    if not fetched:
        return "no fetched data", None
    return fetched, company_name


def analyze_job_posting(posting_text: str, fetched_data: str, cv_text: str, client, model: str, company_name: str | None = None) -> dict:
    try:
        logger.info("Analyzing job posting: %s", posting_text)

        company_instruction = ""
        if company_name:
            company_instruction = f"\n\nIMPORTANT: The hiring company name is: {company_name}\nUse this exact company name in your response."
        else:
            company_instruction = "\n\nFor the 'company' field: Extract the company name from the <fetched_data> section. If not found there, extract it from the <job_posting> section. If completely unknown, write 'Unknown'."

        response = client.chat(
            model=model,
            format=JobMatchingResult.model_json_schema(),
            messages=[
                {"role": "system", "content": JOB_MATCHING_ASSISTANT},
                {
                    "role": "user",
                    "content": f"""
                        Please evaluate the following candidate CV against the job posting and fetched data.

                        <candidate_cv>
                        {cv_text}
                        </candidate_cv>

                        <job_posting>
                        {posting_text}
                        </job_posting>

                        <fetched_data>
                        {fetched_data}
                        </fetched_data>
                        {company_instruction}

                        Provide your assessment in the exact JSON format specified in your system instructions.""",
                },
            ],
            options={"temperature": 0.0,
                     "num_ctx": 16384},
            keep_alive="1m"
        )

        content_str = response["message"]["content"]
        result_dict = json.loads(content_str)

        if company_name:
            result_dict["company"] = company_name

        validated_result = JobMatchingResult(**result_dict)
        return validated_result.model_dump()
    except Exception:
        logger.exception("analyze_job_posting failed")
        return {"odds": 0}


CV_PATH = os.path.join(os.path.dirname(__file__), "files", "my_cv.pdf")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:e2b")

CV_TEXT = ""
ollama_client = Client(host=OLLAMA_URL)

llm_lock = Lock()  # one LLM call at a time


class JobPosting(BaseModel):
    text: str


def clean_cv(raw_cv_text, client, model: str):
    try:
        logger.info("Cleaning CV text (%d chars)", len(raw_cv_text))
        response = client.chat(
            model=model,
            messages=[
                {"role": "system", "content": CV_ORGANIZER},
                {
                    "role": "user",
                    "content": f"""
                        Here is the raw text extracted from the PDF resume. Clean, anonymize, and structure it according to your system instructions.

                        <raw_resume_text>
                        {raw_cv_text}
                        </raw_resume_text>
                        """,
                },
            ],
            options={"temperature": 0.0,
                     "num_ctx": 16384},
            keep_alive=0
        )
        return response["message"]["content"]
    except Exception:
        logger.exception("Failed to clean CV text, returning raw text")
        return raw_cv_text


def load_cv() -> None:
    global CV_TEXT
    cv_clean_text_path = os.path.join(os.path.dirname(__file__), "files", "cv_clean_text.txt")
    if os.path.exists(cv_clean_text_path):
        with open(cv_clean_text_path, "r") as f:
            CV_TEXT = f.read()
        return
    try:
        raw_cv_text = extract_cv_text(CV_PATH)
        logger.info("Loaded raw CV text (%d chars) from %s", len(raw_cv_text), CV_PATH)
        CV_TEXT = clean_cv(raw_cv_text, ollama_client, OLLAMA_MODEL)
        logger.info("Cleaned raw CV text (%d chars)", len(CV_TEXT))
    except Exception:
        logger.exception("Failed to load CV from %s", CV_PATH)
        CV_TEXT = ""


@asynccontextmanager
async def lifespan(app):
    load_cv()
    yield


app = FastAPI(lifespan=lifespan)


@app.post("/analyze")
def analyze(posting: JobPosting) -> dict:
    fetched_data, company_name = extract_fetched_data(posting.text)
    with llm_lock:
        return analyze_job_posting(posting.text, fetched_data, CV_TEXT, ollama_client, OLLAMA_MODEL, company_name)
