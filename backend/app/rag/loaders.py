from pathlib import Path

from pydantic import BaseModel
from pypdf import PdfReader

SUPPORTED_EXTENSIONS = {".pdf", ".txt", ".md"}


class PageText(BaseModel):
    text: str
    page: int | None = None  # 1-based; only set for PDFs


def load_document(path: Path) -> list[PageText]:
    """Extract text from a PDF, TXT or MD file. Empty pages are dropped."""
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {path.suffix!r} ({path.name})")
    if suffix == ".pdf":
        reader = PdfReader(path)
        pages = [
            PageText(text=(page.extract_text() or "").strip(), page=number)
            for number, page in enumerate(reader.pages, start=1)
        ]
    else:
        pages = [PageText(text=path.read_text(encoding="utf-8").strip())]
    return [p for p in pages if p.text]
