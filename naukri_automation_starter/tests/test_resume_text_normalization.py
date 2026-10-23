from pathlib import Path

from naukri_automation import resume
from naukri_automation.application_review_pack import _excerpt_skills


def test_repairs_pdf_word_boundaries_without_changing_content_or_technical_terms():
    extracted = "andM.Techcandidate atNIT Bhopal; ML / AI:PyTorch; HuggingFace; C#; LLM; M.T echscholar"
    normalized = resume.normalize_extracted_resume_text(extracted)

    assert normalized == "and M.Tech candidate at NIT Bhopal; ML / AI: PyTorch; HuggingFace; C#; LLM; M.Tech scholar"
    assert "".join(normalized.split()) == "".join(extracted.split())
    assert "HuggingFace" in normalized
    assert "C#" in normalized
    assert "LLM" in normalized
    assert "M.Tech" in normalized


def test_pdf_extraction_applies_boundary_normalization(tmp_path, monkeypatch):
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"test pdf placeholder")

    class Page:
        def extract_text(self):
            return "andM.Techcandidate\nML / AI:PyTorch"

    class Reader:
        def __init__(self, _path):
            self.pages = [Page()]

    monkeypatch.setattr(resume, "PdfReader", Reader)
    text = resume.extract_resume_text(pdf)
    assert text == "and M.Tech candidate\nML / AI: PyTorch"


def test_readable_review_excerpt_preserves_technical_identifier():
    excerpts = _excerpt_skills(
        "ML / AI: PyTorch, HuggingFace\nC# and LLM applications using M.Tech research",
        {"PyTorch"},
    )
    assert excerpts
    assert "ML / AI: PyTorch, HuggingFace" in excerpts[0]["text"]
