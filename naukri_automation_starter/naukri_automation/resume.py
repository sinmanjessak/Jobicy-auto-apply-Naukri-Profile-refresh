from pathlib import Path
import re
from pypdf import PdfReader


# Some PDFs put adjacent words in separate text-show operations without a
# whitespace glyph. Repair only boundaries supported by a grammatical
# connector, a known degree label, or a label colon; never split general
# CamelCase names or technical tokens.
_JOINED_CONNECTOR = re.compile(
    r"\b(and|at|in|to|of|for|with)(?=(?:M\.Tech|NIT|Python|PyTorch|HuggingFace|LLM|C#))"
)
_SPLIT_MTECH = re.compile(r"M\.T\s+ech(?=(?:scholar|candidate|student)\b|\b)")
_JOINED_DEGREE = re.compile(r"(M\.Tech)(?=(?:candidate|scholar|student)\b)")
_COLON_BEFORE_VALUE = re.compile(r":(?=[A-Za-z0-9])")


def normalize_extracted_resume_text(text: str) -> str:
    """Restore high-confidence PDF word boundaries using whitespace only."""
    text = _SPLIT_MTECH.sub("M.Tech", text)
    text = _JOINED_CONNECTOR.sub(r"\1 ", text)
    text = _JOINED_DEGREE.sub(r"\1 ", text)
    return _COLON_BEFORE_VALUE.sub(": ", text)


def extract_resume_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Resume not found at {path}. Put your PDF at this path or update RESUME_PATH in .env")
    if path.suffix.lower() == ".pdf":
        extracted = "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
        return normalize_extracted_resume_text(extracted)
    if path.suffix.lower() in {".txt", ".md"}:
        return path.read_text(encoding="utf-8")
    raise ValueError("MVP supports PDF, TXT, or Markdown resumes. DOCX support can be added next.")
