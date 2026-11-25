import json
import os
import re
from collections import deque
from pprint import pprint
from threading import Lock

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


def fetch_url_text(url: str, timeout: int = 30) -> tuple[str, str | None]:
    # Standard HTTP fetch with BeautifulSoup
    try:
        response = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
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

app = FastAPI()

job_queue = deque(maxlen=5)
queue_lock = Lock()
BATCH_SIZE = 5


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


@app.on_event("startup")
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


def process_batch() -> list:
    with queue_lock:
        if not job_queue:
            return []
        batch = list(job_queue)
        job_queue.clear()

    results = []
    for text in batch:
        try:
            fetched_data, company_name = extract_fetched_data(text)
            result = analyze_job_posting(text, fetched_data, CV_TEXT, ollama_client, OLLAMA_MODEL, company_name)
            results.append(result)
        except Exception:
            logger.exception("Failed to process posting in batch")
            results.append({"odds": 0})

    return results


@app.post("/analyze")
def analyze(posting: JobPosting) -> dict:
    with queue_lock:
        job_queue.append(posting.text)
        queue_size = len(job_queue)

    if queue_size == BATCH_SIZE:
        logger.info("Queue full (%d items), processing batch", BATCH_SIZE)
        results = process_batch()
        return {"batch_results": results, "count": len(results)}

    logger.info("Added to queue (%d/%d items)", queue_size, BATCH_SIZE)
    return {"queued": True, "queue_size": queue_size, "batch_size": BATCH_SIZE}


@app.post("/analyze/flush")
def flush_queue() -> dict:
    with queue_lock:
        queue_size = len(job_queue)

    if queue_size == 0:
        logger.info("Queue is empty, nothing to process")
        return {"processed": 0, "message": "queue was empty"}

    logger.info("Flushing queue with %d items", queue_size)
    results = process_batch()
    return {"batch_results": results, "count": len(results)}

if __name__ == "__main__":
    cv_clean_text_path = os.path.join(os.path.dirname(__file__), "files", "cv_clean_text.txt")
    with open(cv_clean_text_path, "r") as f:
        TEST_CV_TEXT = f.read()
    posting_text = """
    היי חברים, אנחנו Chamelio, סטארטאפ בתחום הlegal-tech שמפתח פלטפורמת AI לצוותים משפטיים בחברות טק.

אנחנו אחרי סבב seed וכבר עם לקוחות כמו wiz, Appsflyer, Monday ועוד.
אנחנו גדלים ומחפשים סניור דבאופס.
 המשרה היא לא remote, יש ימים מהבית אבל בבקשה לא לפנות אם אין אפשרות בכלל להגיע למשרד (נמצא באיזור הבורסה)

מי שרלוונטי לו מוזמן לדבר איתי 🙂

https://www.linkedin.com/jobs/view/4437267071/
    """
    fetched_data, company_name = extract_fetched_data(posting_text)
    result = analyze_job_posting(posting_text, fetched_data, TEST_CV_TEXT, ollama_client, OLLAMA_MODEL, company_name)
    pprint(result)

