"""Conservative, local evidence generation for two supplied standard documents.

This is a text comparison, not a determination of regulatory equivalence or a
claim that the supplied documents are complete/authentic standard editions.
No network or language-model calls are made.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_HEADING = re.compile(r"^\s*((?:[A-Z]\.\d+|\d{1,3})(?:\.\d+){0,7})\s+(.{1,180})$")
_ANNEX = re.compile(r"^\s*(?:annex|appendix)\s+([A-Z])\b(.*)$", re.I)
_NUMBER = re.compile(r"(?<!\w)[+−-]?\d+(?:[.,]\d+)*(?:[eE][+-]?\d+)?")
_OPERATOR = re.compile(r"<=|>=|!=|==|[<>≤≥≠=±]")
_MODAL = re.compile(r"\b(?:shall|should|must|may|can|will|required|recommended)\b|必须|必須|应当|應當|应|應|可以", re.I)
_NEGATION = re.compile(r"\b(?:not|no|never|except|unless|without|prohibited)\b|不得|禁止|不应|不應", re.I)
_LOGIC = re.compile(r"\b(?:and|or|either|both|all|any)\b|和|或|且", re.I)
# Preserve unit case: mS and MS, for example, need not have the same meaning.
_UNIT = re.compile(r"(?<!\w)[+−-]?\d+(?:[.,]\d+)*(?:[eE][+-]?\d+)?\s*([A-Za-zµμΩ°%‰²³/]+|[\u4e00-\u9fff]+)")


@dataclass
class _Unit:
    text: str
    locator: str
    kind: str = "text"


@dataclass
class _Section:
    clause: str
    title: str
    units: list[_Unit] = field(default_factory=list)
    role: str = "body"

    @property
    def text(self) -> str:
        return "\n".join(unit.text for unit in self.units)

    @property
    def locator(self) -> str:
        if not self.units:
            return ""
        first, last = self.units[0].locator, self.units[-1].locator
        return first if first == last else f"{first} → {last}"


@dataclass
class _Document:
    metadata: dict[str, Any]
    units: list[_Unit] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    partial: bool = False


def _extract(path: Path, reference: str) -> _Document:
    path = Path(path)
    result = _Document({"file_name": path.name, "reference": reference,
                        "format": path.suffix.lower(), "status": "error",
                        "identity_verified": False})
    try:
        data = path.read_bytes()
        result.metadata.update(sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
        if path.suffix.lower() == ".txt":
            encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
            content = data.decode(encoding)
            result.units = [_Unit(line, f"line {i}") for i, line in enumerate(content.splitlines(), 1)]
        elif path.suffix.lower() == ".docx":
            _extract_docx(path, result)
        elif path.suffix.lower() == ".pdf":
            _extract_pdf(path, result)
        else:
            raise ValueError("Unsupported format; supply TXT, DOCX, or a text-based PDF.")
        if not any(unit.text.strip() for unit in result.units):
            raise ValueError("No readable text was extracted; the file may be empty or scanned. OCR/manual review is required.")
        result.metadata.update(status="partial" if result.partial else "extracted",
                               extracted_text_characters=sum(len(unit.text) for unit in result.units),
                               extracted_units=len(result.units))
    except Exception as exc:
        result.metadata.update(status="error", error=f"{type(exc).__name__}: {exc}")
        result.partial = True
        result.warnings.append(result.metadata["error"])
    return result


def _extract_docx(path: Path, result: _Document) -> None:
    try:
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as exc:
        raise RuntimeError("DOCX extraction requires the optional python-docx package.") from exc
    document = Document(path)
    paragraph_count = table_count = 0
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph_count += 1
            result.units.append(_Unit(Paragraph(child, document).text, f"paragraph {paragraph_count}", "paragraph"))
        elif tag == "tbl":
            table_count += 1
            table = Table(child, document)
            for row_number, row in enumerate(table.rows, 1):
                text = "\t".join(cell.text for cell in row.cells)
                result.units.append(_Unit(text, f"table {table_count}, row {row_number}", "table"))
    tags = [node.tag.rsplit("}", 1)[-1] for node in document.element.iter()]
    unresolved = sum(tags.count(tag) for tag in ("drawing", "pict", "oMath", "altChunk", "ins", "del", "footnoteReference", "endnoteReference", "txbxContent"))
    numbered = tags.count("numPr")
    if unresolved or numbered:
        result.partial = True
        result.warnings.append("DOCX contains drawings, equations, tracked changes, notes, text boxes, or automatic numbering that this extractor does not fully reconstruct.")
    if any("tbl" == node.tag.rsplit("}", 1)[-1] for table in document.tables for row in table.rows for cell in row.cells for node in cell._tc.iterchildren()):
        result.partial = True
        result.warnings.append("Nested DOCX tables require manual review; their content is not fully extracted.")
    if table_count:
        result.warnings.append("DOCX tables are compared as ordered rows/cell text; merged-cell layout and table meaning require review.")
    result.warnings.append("DOCX body text is extracted in order. Headers, footers, comments, and rendered pagination are outside this extraction scope.")
    result.metadata.update(paragraphs=paragraph_count, tables=table_count,
                           unresolved_objects=unresolved, automatic_numbered_paragraphs=numbered)


def _extract_pdf(path: Path, result: _Document) -> None:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("PDF extraction requires the optional pypdf package.") from exc
    reader = PdfReader(path)
    if reader.is_encrypted:
        raise ValueError("Encrypted PDFs are not supported; provide an authorized readable copy.")
    empty_pages: list[int] = []
    failed_pages: list[int] = []
    for page_number, page in enumerate(reader.pages, 1):
        try:
            content = page.extract_text() or ""
        except Exception:
            failed_pages.append(page_number)
            continue
        if not content.strip():
            empty_pages.append(page_number)
        result.units.extend(_Unit(line, f"PDF page {page_number}, line {line_number}")
                            for line_number, line in enumerate(content.splitlines(), 1))
    # PDF text extraction cannot establish that figures, mathematical glyphs and
    # table structure were preserved, even when every page yielded some text.
    result.partial = True
    result.metadata.update(pages=len(reader.pages), empty_pages=empty_pages, failed_pages=failed_pages)
    result.warnings.append("PDF comparison covers extracted page text only. Reading order, figures, equations, table structure, and OCR completeness remain unverified.")
    if empty_pages or failed_pages:
        result.warnings.append(f"PDF pages with no text: {empty_pages}; failed pages: {failed_pages}. These pages require visual/OCR review.")


def _sections(document: _Document) -> list[_Section]:
    sections: list[_Section] = []
    current = _Section("document-start", "Unnumbered opening text")
    role = "body"
    for unit in document.units:
        annex = _ANNEX.match(unit.text) if unit.kind != "table" else None
        heading = _HEADING.match(unit.text) if unit.kind != "table" else None
        if annex or heading:
            if current.text.strip():
                sections.append(current)
            if annex:
                suffix = annex.group(2)
                role = "informative" if re.search(r"\binformative\b", suffix, re.I) else (
                    "normative" if re.search(r"\bnormative\b", suffix, re.I) else "unspecified")
                current = _Section(f"Annex {annex.group(1).upper()}", suffix.strip(), role=role)
            else:
                current = _Section(heading.group(1), heading.group(2), role=role)
        elif current.clause.startswith("Annex ") and len(current.units) <= 1:
            marker = unit.text.strip().lower().strip("()")
            if marker in {"normative", "informative"}:
                role = current.role = marker
        current.units.append(unit)
    if current.text.strip():
        sections.append(current)
    return sections


def _comparison_text(section: _Section) -> str:
    """Remove only a recognized heading's number for critical-content checks."""
    lines = section.text.splitlines()
    if lines:
        match = _HEADING.match(lines[0])
        if match:
            lines[0] = match.group(2)
    return "\n".join(lines)


def _word_overlap(left: str, right: str) -> float:
    a = set(re.findall(r"\w+", left.casefold()))
    b = set(re.findall(r"\w+", right.casefold()))
    return len(a & b) / max(1, len(a | b))


def _critical(left: str, right: str) -> list[str]:
    expressions = {"number": _NUMBER, "operator": _OPERATOR, "negation": _NEGATION,
                   "modality": _MODAL, "unit": _UNIT, "logic": _LOGIC}
    return [name for name, expression in expressions.items()
            if expression.findall(left) != expression.findall(right)]


def _match_sections(old: list[_Section], new: list[_Section]) -> list[tuple[_Section | None, _Section | None, str]]:
    """Only unique structural IDs or unique exact titles make pairing candidates."""
    pairs: list[tuple[_Section | None, _Section | None, str]] = []
    used_old: set[int] = set()
    used_new: set[int] = set()
    old_counts = Counter(section.clause for section in old)
    new_counts = Counter(section.clause for section in new)
    old_by_id = {section.clause: i for i, section in enumerate(old)}
    common_ids = {key for key, count in old_counts.items() if count == new_counts[key] == 1}
    old_order = {key: i for i, key in enumerate(s.clause for s in old if s.clause in common_ids)}
    new_order = {key: i for i, key in enumerate(s.clause for s in new if s.clause in common_ids)}
    for j, right in enumerate(new):
        if old_counts[right.clause] == new_counts[right.clause] == 1:
            i = old_by_id[right.clause]
            left = old[i]
            title_matches = left.title.casefold() == right.title.casefold()
            overlap = _word_overlap(_comparison_text(left), _comparison_text(right))
            method = "structural_candidate" if title_matches and overlap >= 0.35 else "mapping_uncertain"
            if method == "structural_candidate" and old_order[right.clause] != new_order[right.clause]:
                method = "relocation_candidate"
            pairs.append((left, right, method))
            used_old.add(i)
            used_new.add(j)
    for j, right in enumerate(new):
        if j in used_new:
            continue
        candidates = [i for i, left in enumerate(old) if i not in used_old
                      and left.title.strip() and left.title.casefold() == right.title.casefold()]
        same_new_title = [k for k, section in enumerate(new) if k not in used_new
                          and section.title.casefold() == right.title.casefold()]
        if len(candidates) == len(same_new_title) == 1:
            i = candidates[0]
            if _word_overlap(_comparison_text(old[i]), _comparison_text(right)) >= 0.5:
                pairs.append((old[i], right, "renumbering_candidate"))
                used_old.add(i)
                used_new.add(j)
    pairs.extend((left, None, "unmatched_or_split_merge") for i, left in enumerate(old) if i not in used_old)
    pairs.extend((None, right, "unmatched_or_split_merge") for j, right in enumerate(new) if j not in used_new)
    return pairs


def compare_standard_files(old_path: Path | str, new_path: Path | str, *, old_reference: str, new_reference: str) -> dict[str, Any]:
    """Return auditable text-change candidates; never auto-approve a comparison.

Statuses: comparison_rejected (identical bytes), comparison_incomplete (failed or
partial extraction), review_required (candidate changes), or
no_textual_differences_found (only within the supplied extracted text).
"""
    old_doc = _extract(Path(old_path), str(old_reference))
    new_doc = _extract(Path(new_path), str(new_reference))
    warnings = [f"Old document: {warning}" for warning in old_doc.warnings]
    warnings += [f"New document: {warning}" for warning in new_doc.warnings]
    warnings.append("Results are text-change candidates requiring review. Standard identity, supplied-document completeness, and legal/technical equivalence are not verified.")
    result: dict[str, Any] = {
        "schema_version": "standards-diff-v1", "status": "comparison_incomplete", "changes": [],
        "coverage": {"scope": "supplied extracted text", "semantic_completeness_verified": False,
                     "extraction_complete": False, "unresolved_mappings": 0},
        "warnings": warnings, "old_document": old_doc.metadata, "new_document": new_doc.metadata,
    }
    old_hash, new_hash = old_doc.metadata.get("sha256"), new_doc.metadata.get("sha256")
    if old_doc.metadata["status"] == "error" or new_doc.metadata["status"] == "error":
        warnings.append("A source could not be extracted. No conclusion about absent changes is possible.")
        return result
    if old_hash and old_hash == new_hash:
        result["status"] = "comparison_rejected"
        warnings.append("Both inputs contain identical bytes (same SHA-256); provide two distinct source editions.")
        return result
    old_sections, new_sections = _sections(old_doc), _sections(new_doc)
    pairs = _match_sections(old_sections, new_sections)
    for left, right, method in pairs:
        if left and right and left.text == right.text and method != "relocation_candidate":
            continue
        old_text, new_text = left.text if left else "", right.text if right else ""
        critical = _critical(_comparison_text(left) if left else "", _comparison_text(right) if right else "")
        if method in {"mapping_uncertain", "unmatched_or_split_merge", "renumbering_candidate", "relocation_candidate"}:
            change_type = method
        elif critical:
            change_type = "critical_text_change"
        elif re.sub(r"\s+", " ", old_text).strip() == re.sub(r"\s+", " ", new_text).strip():
            change_type = "whitespace_change_candidate"
        else:
            change_type = "text_change_candidate"
        row = {"old_clause": left.clause if left else "", "new_clause": right.clause if right else "",
               "old_text": old_text, "new_text": new_text, "change_type": change_type,
               "critical_tokens_changed": critical, "review_required": True,
               "old_locator": left.locator if left else "", "new_locator": right.locator if right else "",
               "old_role": left.role if left else "", "new_role": right.role if right else "",
               "mapping_method": method}
        digest = hashlib.sha256(json.dumps([old_hash, new_hash, old_reference, new_reference, row], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        row["id"] = f"text-change-{digest[:20]}"
        result["changes"].append(row)
    unknown_annexes = [s.clause for s in old_sections + new_sections if s.role == "unspecified"]
    duplicate_ids = any(count > 1 for sections in (old_sections, new_sections) for count in Counter(s.clause for s in sections).values())
    unresolved = sum(method in {"mapping_uncertain", "unmatched_or_split_merge"} for _, _, method in pairs)
    if unknown_annexes:
        warnings.append("Annex role is unspecified for some extracted sections; their content remains included for review.")
    if duplicate_ids:
        warnings.append("Repeated clause identifiers were found; automatic correspondence is ambiguous (possible contents/headers).")
    if unresolved:
        warnings.append("Uncertain or unmatched sections may represent additions, deletions, restructuring, splits, or merges; the tool does not decide which without review.")
    partial = old_doc.partial or new_doc.partial or duplicate_ids
    result["coverage"].update(extraction_complete=not partial, old_sections=len(old_sections),
                              new_sections=len(new_sections), matched_sections=sum(bool(a and b) for a, b, _ in pairs),
                              unresolved_mappings=unresolved, unknown_annex_roles=bool(unknown_annexes),
                              all_extracted_sections_accounted_for=True)
    result["status"] = "comparison_incomplete" if partial else (
        "review_required" if result["changes"] else "no_textual_differences_found")
    if not result["changes"]:
        warnings.append("No textual differences were found in the extracted scope; this does not establish that the editions have no substantive changes.")
    return result


__all__ = ["compare_standard_files"]
