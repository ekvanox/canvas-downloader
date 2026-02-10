"""Deduplicate PDF files based on text content similarity.

Extracts text from each PDF using PyMuPDF, then compares all pairs using
the Ratcliff/Obershelp algorithm (difflib.SequenceMatcher).  Files whose
text content is >= 95% similar to an already-kept file are removed.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path

import fitz  # PyMuPDF


_SIMILARITY_THRESHOLD = 0.95


def _extract_text(pdf_path: Path) -> str:
    """Extract all text from a PDF file."""
    try:
        doc = fitz.open(str(pdf_path))
        text = "".join(page.get_text("text") for page in doc)  # type: ignore[arg-type]
        doc.close()
        return text.strip()
    except Exception:
        return ""


def _similarity(a: str, b: str) -> float:
    """Return the similarity ratio between two strings (0.0–1.0).

    Uses SequenceMatcher with quick_ratio() as a fast pre-filter.
    """
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0

    sm = SequenceMatcher(None, a, b, autojunk=False)

    # Fast upper-bound check — skip the expensive real_quick_ratio/ratio
    # computation if the texts are clearly different.
    if sm.quick_ratio() < _SIMILARITY_THRESHOLD:
        return sm.quick_ratio()

    return sm.ratio()


def deduplicate_pdfs(directory: Path, threshold: float = _SIMILARITY_THRESHOLD) -> int:
    """Remove near-duplicate PDFs from *directory*.

    Compares every pair of PDFs by extracted text content.  When two files
    are >= *threshold* similar, the one with the longer filename (or
    alphabetically later) is deleted.

    Returns the number of files removed.
    """
    pdf_files = sorted(directory.glob("*.pdf"))
    if len(pdf_files) < 2:
        return 0

    print(f"  Deduplicating {len(pdf_files)} PDF(s) (threshold: {threshold:.0%})...")

    # Extract text from all PDFs
    texts: dict[Path, str] = {}
    for pdf in pdf_files:
        texts[pdf] = _extract_text(pdf)

    removed: set[Path] = set()
    removed_count = 0

    for i, pdf_a in enumerate(pdf_files):
        if pdf_a in removed:
            continue
        for pdf_b in pdf_files[i + 1:]:
            if pdf_b in removed:
                continue

            text_a = texts[pdf_a]
            text_b = texts[pdf_b]

            # Skip comparison if both are empty/near-empty
            if len(text_a) < 20 and len(text_b) < 20:
                continue

            sim = _similarity(text_a, text_b)
            if sim >= threshold:
                # Remove the one with the longer name (less descriptive title
                # tends to be a sub-page or duplicate view).  Tie-break
                # alphabetically.
                if len(pdf_b.name) >= len(pdf_a.name):
                    victim = pdf_b
                else:
                    victim = pdf_a

                print(
                    f"    Removing duplicate: {victim.name} "
                    f"(~{sim:.0%} similar to {(pdf_a if victim == pdf_b else pdf_b).name})"
                )
                victim.unlink()
                removed.add(victim)
                removed_count += 1

                # If we removed pdf_a, stop comparing it
                if victim == pdf_a:
                    break

    if removed_count:
        print(f"  Dedup: removed {removed_count} duplicate(s).")
    else:
        print("  Dedup: no duplicates found.")

    return removed_count
