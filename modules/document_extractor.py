from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET

import pandas as pd
from openpyxl import load_workbook

from .reference_graph import (
    deterministic_reference_range,
    document_code_from_filename,
    document_type_from_code,
    extract_external_references,
    extract_header_metadata,
    extract_internal_codes,
    extract_reference_items,
    plain_text_reference_range,
)

try:
    from docx import Document
except ImportError:  # pragma: no cover - fallback is exercised when dependency is absent.
    Document = None


REG_REFERENCE_RE = re.compile(
    r"(?:BS\s+EN\s+)?(?:EN\s+)?ISO\s*\d+(?:-\d+)?(?::\d{4})?"
    r"|IEC\s*\d+(?::\d{4})?"
    r"|YY/T\s*\d+"
    r"|Regulation\s*\(?EU\)?\s*2017/745"
    r"|(?:EU\s+)?MDR"
    r"|MDD\s*93/42/EEC"
    r"|93/42/EEC"
    r"|21\s*CFR"
    r"|510\(k\)"
    r"|GUDID|EUDAMED|MDACS|GN-\d+|COP",
    re.IGNORECASE,
)
SECTION_HEADING_RE = re.compile(
    r"^\s*(?:(?P<number>[1-9](?:\.\d+)*)\s*)?(?P<title>Purpose|Scope|References?|Definition|Responsibilit(?:y|ies)|Procedure|Record(?:s)?(?:\s+and\s+Form)?|Form(?:s)?)(?:\s*/.*)?\s*$",
    re.IGNORECASE,
)
DOC_CODE_RE = re.compile(r"\b(?:QSP\d{4}|QM\d{2}|WI-\d{2}-[A-Z]{3}-\d{2}|FM\d+[A-Z]?|SOP\d{3,4}|CP\d{3,4}|FORM\d{3,4})\b", re.IGNORECASE)
GUIDANCE_RE = re.compile(r"\bMDCG\s*\d{4}-\d+\b|\bGN-\d+\b|\bCOP\b|\bMDACS\b", re.IGNORECASE)
FORM_RE = re.compile(r"\bFM\d+[A-Z]?\b", re.IGNORECASE)
DEFAULT_ANNOTATION_PATH = Path(__file__).resolve().parents[1] / "data" / "section3_annotations.json"
DEFAULT_ORIGINAL_FILE_DIR = Path(__file__).resolve().parents[2] / "original file"
REFERENCE_DOCUMENT_MARKERS = [
    "參考文件 Reference Document",
    "參考文件 References Document",
    "參考文件 References",
    "參考文件",
]
CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")
CONTROLLED_CODE_RE = re.compile(r"\b(?:QSP|QM|WI|FM|SOP|CP|FORM)[A-Z0-9\-]*\b", re.IGNORECASE)
SECTION_NUMBER_RE = re.compile(r"^\s*(?:[0-9]+(?:\.[0-9]+)*|[（(][0-9]+[）)]|[A-Za-z][).]?)")
KEEP_SYMBOLS = set(
    " \t.、，。：:；;（）()/\\-—≤≥<>=+%&《》“”‘’[]【】|•"
)


def normalize_reference_text(value: str) -> str:
    return re.sub(r"[^A-Z0-9\u4e00-\u9fff]+", "", str(value or "").upper())


def original_file_index(original_file_dir: str | Path = DEFAULT_ORIGINAL_FILE_DIR) -> dict[str, list[dict[str, str]]]:
    root = Path(original_file_dir)
    index: dict[str, list[dict[str, str]]] = {"by_code": {}, "files": []}  # type: ignore[assignment]
    if not root.exists() or not root.is_dir():
        return index
    files = []
    by_code: dict[str, list[dict[str, str]]] = {}
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.name.startswith("~$"):
            continue
        code = document_code_from_filename(path.name)
        record = {
            "document_code": code,
            "file_name": path.name,
            "file_path": str(path),
            "normalized_name": normalize_reference_text(path.stem),
        }
        files.append(record)
        by_code.setdefault(code.upper(), []).append(record)
    index["by_code"] = by_code  # type: ignore[assignment]
    index["files"] = files  # type: ignore[assignment]
    return index


def extract_docx_sections(path: str | Path) -> list[dict[str, object]]:
    path = Path(path)
    sections: list[dict[str, object]] = []
    if Document is not None:
        doc = Document(path)
        for section_index, doc_section in enumerate(doc.sections, start=1):
            for header_index, paragraph in enumerate(doc_section.header.paragraphs, start=1):
                text = paragraph.text.strip()
                if text:
                    sections.append(_section(f"H{section_index}P{header_index}", "header", text))
            for footer_index, paragraph in enumerate(doc_section.footer.paragraphs, start=1):
                text = paragraph.text.strip()
                if text:
                    sections.append(_section(f"F{section_index}P{footer_index}", "footer", text))
        paragraph_index = 0
        table_index = 0
        for child in doc.element.body.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                paragraph_index += 1
                text = "".join(node.text or "" for node in child.iter() if node.tag.rsplit("}", 1)[-1] == "t").strip()
                if text:
                    sections.append(_section(f"P{paragraph_index}", "paragraph", text))
            elif tag == "tbl":
                table_index += 1
                row_index = 0
                for row in child.iter():
                    if row.tag.rsplit("}", 1)[-1] != "tr":
                        continue
                    row_index += 1
                    cells = []
                    for cell in row.iter():
                        if cell.tag.rsplit("}", 1)[-1] != "tc":
                            continue
                        cell_text = "".join(node.text or "" for node in cell.iter() if node.tag.rsplit("}", 1)[-1] == "t").strip()
                        if cell_text:
                            cells.append(cell_text)
                    text = " | ".join(cells)
                    if text:
                        sections.append(_section(f"T{table_index}R{row_index}", "table", text))
        return sections

    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    block_index = 0
    for node in root.iter():
        if node.tag not in {ns + "p", ns + "tr"}:
            continue
        block_index += 1
        text = "".join(part.text or "" for part in node.iter(ns + "t")).strip()
        if text:
            sections.append(_section(f"B{block_index}", "paragraph/table", text))
    return sections


def extract_xlsx_sections(path: str | Path) -> list[dict[str, object]]:
    wb = load_workbook(path, read_only=True, data_only=True)
    sections: list[dict[str, object]] = []
    for ws in wb.worksheets:
        for row_index, row in enumerate(ws.iter_rows(values_only=True), start=1):
            text = " | ".join(str(cell).strip() for cell in row if cell is not None and str(cell).strip())
            if text:
                sections.append(_section(f"{ws.title}!R{row_index}", f"sheet {ws.title}", text))
    return sections


def extract_xls_sections(path: str | Path) -> list[dict[str, object]]:
    try:
        sheets = pd.read_excel(path, sheet_name=None, header=None)
    except ImportError as exc:
        raise ValueError("Unsupported .xls extraction: install xlrd to read legacy Excel files.") from exc
    sections: list[dict[str, object]] = []
    for sheet_name, frame in sheets.items():
        for row_index, row in frame.iterrows():
            values = [str(value).strip() for value in row.tolist() if pd.notna(value) and str(value).strip()]
            if values:
                sections.append(_section(f"{sheet_name}!R{row_index + 1}", f"sheet {sheet_name}", " | ".join(values)))
    return sections


def extract_plain_text(path: str | Path, sections: list[dict[str, object]]) -> str:
    file_path = Path(path)
    if file_path.suffix.lower() in {".doc", ".docx"}:
        textutil = shutil.which("textutil")
        if textutil:
            result = subprocess.run(
                [textutil, "-convert", "txt", "-stdout", str(file_path)],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                timeout=60,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
    if file_path.suffix.lower() in {".xlsx", ".xls"}:
        return "\n".join(str(section.get("text", "")) for section in sections)
    return "\n".join(str(section.get("text", "")) for section in sections)


def has_chinese(text: str) -> bool:
    return bool(CHINESE_RE.search(str(text or "")))


def controlled_codes(text: str) -> list[str]:
    return [match.group(0) for match in CONTROLLED_CODE_RE.finditer(str(text or ""))]


def clean_chinese_only_line(text: str) -> tuple[str, list[str]]:
    raw = str(text or "")
    codes = controlled_codes(raw)
    spans = [(match.start(), match.end(), match.group(0)) for match in CONTROLLED_CODE_RE.finditer(raw)]
    output: list[str] = []
    index = 0
    span_index = 0

    while index < len(raw):
        if span_index < len(spans) and index == spans[span_index][0]:
            output.append(spans[span_index][2])
            index = spans[span_index][1]
            span_index += 1
            continue

        char = raw[index]
        if char.isascii() and char.isalpha():
            index += 1
            while index < len(raw) and (raw[index].isascii() and (raw[index].isalpha() or raw[index] in " ,.'-")):
                index += 1
            continue
        if CHINESE_RE.match(char) or char.isdigit() or char in KEEP_SYMBOLS:
            output.append(" " if char.isspace() else char)
        index += 1

    cleaned = "".join(output)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"\s+([、，。：:；;）)\]】》])", r"\1", cleaned)
    cleaned = re.sub(r"([（(\[【《])\s+", r"\1", cleaned)
    cleaned = re.sub(r"\s*([|])\s*", r" \1 ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    if not has_chinese(cleaned) and not controlled_codes(cleaned):
        return "", codes
    return cleaned, codes


def section_number_from_text(text: str) -> str:
    match = SECTION_NUMBER_RE.match(str(text or ""))
    return match.group(0).strip() if match else ""


def extract_chinese_only_text(raw_text: str, separator: str = "\n") -> tuple[str, list[str], str]:
    cleaned_lines = []
    kept_codes: list[str] = []
    raw_lines = str(raw_text or "").splitlines() or [str(raw_text or "")]
    if separator == " | ":
        table_lines = []
        for raw_line in raw_lines:
            cells = []
            for cell in raw_line.split("|"):
                cleaned_cell, codes = clean_chinese_only_line(cell)
                kept_codes.extend(codes)
                if cleaned_cell:
                    cells.append(cleaned_cell)
            if cells:
                table_lines.append(" | ".join(cells))
        raw_lines = table_lines
    for raw_line in raw_lines:
        cleaned, codes = clean_chinese_only_line(raw_line)
        kept_codes.extend(codes)
        if cleaned:
            cleaned_lines.append(cleaned)

    text = separator.join(cleaned_lines)
    unique_codes = list(dict.fromkeys(kept_codes))
    if has_chinese(text) and unique_codes:
        reason = "kept_chinese_and_controlled_code"
    elif has_chinese(text):
        reason = "kept_chinese"
    elif unique_codes:
        reason = "kept_controlled_code"
    else:
        reason = "discarded_english_only"
    return text, unique_codes, reason


def extract_chinese_only_blocks(document: dict[str, object]) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = []
    source_file = str(document.get("file_path") or document.get("file_name") or "")
    for index, section in enumerate(document.get("sections", []), start=1):
        raw_text = str(section.get("text", ""))
        separator = " | " if str(section.get("page_or_location", "")).startswith("table") else "\n"
        chinese_text, codes, reason = extract_chinese_only_text(raw_text, separator=separator)
        if not chinese_text:
            continue
        blocks.append(
            {
                "source_file": source_file,
                "page_number": None,
                "block_index": index,
                "section_id": str(section.get("section_id", "")),
                "section_number": section_number_from_text(raw_text),
                "raw_text": raw_text,
                "chinese_only_text": chinese_text,
                "kept_codes": codes,
                "has_chinese": has_chinese(chinese_text),
                "extraction_reason": reason,
            }
        )
    return blocks


def chinese_only_markdown(document: dict[str, object], blocks: list[dict[str, object]]) -> str:
    title_parts = [str(document.get("document_code", "")).strip(), str(document.get("document_title", "")).strip()]
    title = " ".join(part for part in title_parts if part).strip() or str(document.get("file_name", "Chinese extraction"))
    lines = [f"# {title}", ""]
    current_page = object()
    for block in blocks:
        page = block.get("page_number")
        if page and page != current_page:
            lines.extend([f"## Page {page}", ""])
            current_page = page
        lines.append(str(block.get("chinese_only_text", "")))
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def extract_document(path: str | Path) -> dict[str, object]:
    file_path = Path(path)
    if file_path.suffix.lower() == ".docx":
        sections = extract_docx_sections(file_path)
    elif file_path.suffix.lower() == ".xlsx":
        sections = extract_xlsx_sections(file_path)
    elif file_path.suffix.lower() == ".xls":
        sections = extract_xls_sections(file_path)
    else:
        raise ValueError(f"Unsupported extracted file type: {file_path.suffix}")

    metadata = extract_header_metadata(file_path.name, sections)
    code = metadata["document_code"]
    main_sections = extract_main_sections(sections)
    plain_text = extract_plain_text(file_path, sections)
    references = extract_section_references(sections, main_sections, file_path=file_path, document_code=code, plain_text=plain_text)
    return {
        "file_path": str(file_path),
        "file_name": file_path.name,
        "document_code": code,
        "document_title": metadata["document_title"],
        "document_type": metadata["document_type"],
        "doc_revision": metadata["doc_revision"],
        "sections": sections,
        "main_sections": main_sections,
        **references,
    }


def refresh_document_references(document: dict[str, object], source_path: str | Path) -> None:
    sections = document.get("sections", [])
    main_sections = document.get("main_sections", [])
    if not isinstance(sections, list) or not isinstance(main_sections, list):
        return

    source = Path(source_path)
    plain_text = extract_plain_text(source, sections)
    references = extract_section_references(
        sections,
        main_sections,
        file_path=source,
        document_code=str(document.get("document_code", "")),
        plain_text=plain_text,
    )
    document.update(references)


def extract_documents(paths: list[str | Path]) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    records = []
    failures = []
    for path in paths:
        try:
            records.append(extract_document(path))
        except Exception as exc:
            failures.append({"file_path": str(path), "reason": str(exc)})
    return records, failures


def _section(section_id: str, location: str, text: str) -> dict[str, object]:
    return {
        "section_id": section_id,
        "page_or_location": location,
        "text": text,
        "contains_regulation_reference": bool(REG_REFERENCE_RE.search(text)),
    }


def extract_main_sections(sections: list[dict[str, object]]) -> list[dict[str, str]]:
    main_sections = []
    for index, section in enumerate(sections):
        text = str(section.get("text", "")).strip()
        match = SECTION_HEADING_RE.match(text)
        if not match:
            continue
        number = match.group("number") or str(len(main_sections) + 1)
        title = match.group("title").strip()
        main_sections.append(
            {
                "section_id": str(section.get("section_id", "")),
                "number": number,
                "title": title,
                "start_index": str(index),
            }
        )
    return main_sections


def extract_section_references(
    sections: list[dict[str, object]],
    main_sections: list[dict[str, str]],
    file_path: str | Path | None = None,
    document_code: str = "",
    annotation_path: str | Path = DEFAULT_ANNOTATION_PATH,
    original_file_dir: str | Path = DEFAULT_ORIGINAL_FILE_DIR,
    plain_text: str = "",
) -> dict[str, list[str]]:
    plain_range = plain_text_reference_range(plain_text) if plain_text else {}
    range_info = (
        {
            "start_index": None,
            "end_index": None,
            "start_section_id": "",
            "end_section_id": "",
            "source": plain_range["source"],
            "annotation_stale": False,
            "reference_text": plain_range["reference_text"],
            "body_start_found": False,
            "section3_found": True,
            "section3_start_method": "plain_text_heading",
            "section3_end_method": "plain_text_stop_heading",
            "extraction_warnings": [],
        }
        if plain_range.get("section3_found")
        else select_reference_range(sections, main_sections, file_path, document_code, annotation_path)
    )
    reference_sections = sections[range_info["start_index"] : range_info["end_index"]] if range_info["start_index"] is not None else []

    reference_text = str(range_info.get("reference_text", "")) or "\n".join(str(section.get("text", "")) for section in reference_sections)
    marker, marker_text = marker_reference_text(reference_text)
    filename_reference_text = marker_text or reference_text
    reference_items = match_reference_items(extract_reference_items(filename_reference_text), original_file_dir)
    internal_refs = extract_internal_codes(filename_reference_text)
    item_codes = [str(item.get("reference_code", "")) for item in reference_items if item.get("reference_code")]
    reference_documents = sorted({code for code in item_codes if not code.startswith("FM")})
    reference_forms = sorted({code for code in item_codes if code.startswith("FM")})
    matched_reference_files = matched_files_from_items(reference_items)
    unmatched_reference_files = [
        str(item.get("reference_code", ""))
        for item in reference_items
        if item.get("reference_code") and item.get("match_status") == "unresolved"
    ]
    external_refs = extract_external_references(reference_text)
    reference_guidance = sorted({item for item in external_refs if re.search(r"\b(?:MDCG|GN-|COP)\b", item, re.I)})
    reference_standards = sorted({item for item in external_refs if re.search(r"\b(?:ISO|IEC|YY/T|GB/T|ASTM|AAMI|JJG|JJF|TR)", item, re.I)})
    reference_regulations = sorted({item for item in external_refs if item not in reference_standards and item not in reference_guidance})
    return {
        "reference_regulations": reference_regulations,
        "reference_standards": reference_standards,
        "reference_guidance": reference_guidance,
        "reference_documents": reference_documents,
        "reference_forms": reference_forms,
        "document_relationships": reference_documents + reference_forms,
        "reference_items": reference_items,
        "internal_reference_details": internal_refs,
        "external_references": external_refs,
        "reference_section_start_id": range_info["start_section_id"],
        "reference_section_end_id": range_info["end_section_id"],
        "reference_detection_source": range_info["source"],
        "reference_annotation_stale": range_info["annotation_stale"],
        "reference_text": reference_text,
        "body_start_found": range_info.get("body_start_found", False),
        "section3_found": range_info.get("section3_found", bool(reference_text)),
        "section3_start_method": range_info.get("section3_start_method", ""),
        "section3_end_method": range_info.get("section3_end_method", ""),
        "extraction_warnings": range_info.get("extraction_warnings", []),
        "reference_document_marker_found": bool(marker_text),
        "reference_document_marker": marker,
        "marker_reference_text": marker_text,
        "matched_reference_files": matched_reference_files,
        "unmatched_reference_files": unmatched_reference_files,
    }


def marker_reference_text(reference_text: str) -> tuple[str, str]:
    matches = [
        (reference_text.find(marker), len(marker), marker)
        for marker in REFERENCE_DOCUMENT_MARKERS
        if marker in reference_text
    ]
    if not matches:
        return "", ""
    start, marker_length, marker = min(matches, key=lambda item: (item[0], -item[1]))
    return marker, reference_text[start + marker_length :].strip()


def match_reference_files(reference_text: str, original_file_dir: str | Path = DEFAULT_ORIGINAL_FILE_DIR) -> tuple[list[dict[str, str]], list[str]]:
    if not reference_text.strip():
        return [], []
    index = original_file_index(original_file_dir)
    by_code: dict[str, list[dict[str, str]]] = index.get("by_code", {})  # type: ignore[assignment]
    files: list[dict[str, str]] = index.get("files", [])  # type: ignore[assignment]
    matched: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    unmatched: list[str] = []

    for code in controlled_codes(reference_text):
        code_key = code.upper()
        records = by_code.get(code_key, [])
        if records:
            for record in records:
                if record["file_path"] not in seen_paths:
                    matched.append({key: record[key] for key in ["document_code", "file_name", "file_path"]})
                    seen_paths.add(record["file_path"])
        else:
            unmatched.append(code)

    normalized_reference = normalize_reference_text(reference_text)
    for record in files:
        if record["file_path"] in seen_paths:
            continue
        normalized_name = record.get("normalized_name", "")
        if normalized_name and (normalized_name in normalized_reference or normalized_reference in normalized_name):
            matched.append({key: record[key] for key in ["document_code", "file_name", "file_path"]})
            seen_paths.add(record["file_path"])

    return matched, list(dict.fromkeys(unmatched))


def match_reference_items(
    reference_items: list[dict[str, str]],
    original_file_dir: str | Path = DEFAULT_ORIGINAL_FILE_DIR,
) -> list[dict[str, object]]:
    index = original_file_index(original_file_dir)
    by_code: dict[str, list[dict[str, str]]] = index.get("by_code", {})  # type: ignore[assignment]
    matched_items: list[dict[str, object]] = []
    for item in reference_items:
        code = str(item.get("reference_code", "")).upper()
        record: dict[str, object] = dict(item)
        if not code:
            record["match_status"] = "no_controlled_code"
            record["matched_files"] = []
            matched_items.append(record)
            continue
        matches = by_code.get(code, [])
        record["match_status"] = "matched" if matches else "unresolved"
        record["matched_files"] = [{key: match[key] for key in ["document_code", "file_name", "file_path"]} for match in matches]
        matched_items.append(record)
    return matched_items


def matched_files_from_items(reference_items: list[dict[str, object]]) -> list[dict[str, str]]:
    matched: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for item in reference_items:
        for match in item.get("matched_files", []) or []:
            if not isinstance(match, dict):
                continue
            file_path = str(match.get("file_path", ""))
            if file_path in seen_paths:
                continue
            seen_paths.add(file_path)
            matched.append(
                {
                    "document_code": str(match.get("document_code", "")),
                    "file_name": str(match.get("file_name", "")),
                    "file_path": file_path,
                }
            )
    return matched


def marker_reference_range(
    sections: list[dict[str, object]],
    main_sections: list[dict[str, str]],
) -> dict[str, object] | None:
    for index, section in enumerate(sections):
        marker, _ = marker_reference_text(str(section.get("text", "")))
        if not marker:
            continue
        end_index = next_heading_index(main_sections, index, len(sections))
        return {
            "start_index": index,
            "end_index": end_index,
            "start_section_id": str(sections[index].get("section_id", "")),
            "end_section_id": str(sections[end_index - 1].get("section_id", "")) if end_index > index else "",
            "source": "marker_reference_document",
            "annotation_stale": False,
        }
    return None


def select_reference_range(
    sections: list[dict[str, object]],
    main_sections: list[dict[str, str]],
    file_path: str | Path | None = None,
    document_code: str = "",
    annotation_path: str | Path = DEFAULT_ANNOTATION_PATH,
) -> dict[str, object]:
    annotation = find_section3_annotation(file_path, document_code, annotation_path)
    if annotation:
        manual_range = annotation_range(sections, annotation)
        if manual_range:
            return manual_range

    deterministic_range = deterministic_reference_range(sections, main_sections)
    if deterministic_range.get("section3_found"):
        deterministic_range["annotation_stale"] = bool(annotation)
        return deterministic_range

    marker_range = marker_reference_range(sections, main_sections)
    if marker_range:
        marker_range["annotation_stale"] = bool(annotation)
        return marker_range

    heading_range = auto_reference_range(sections, main_sections, prefer_numbered=True)
    if heading_range:
        heading_range["annotation_stale"] = bool(annotation)
        return heading_range

    heading_range = auto_reference_range(sections, main_sections, prefer_numbered=False)
    if heading_range:
        heading_range["annotation_stale"] = bool(annotation)
        return heading_range

    return empty_reference_range(annotation_stale=bool(annotation))


def annotation_keys(file_path: str | Path | None, document_code: str = "") -> list[str]:
    keys = []
    if file_path:
        path = Path(file_path)
        try:
            keys.append(str(path.expanduser().resolve()))
        except OSError:
            keys.append(str(path.expanduser()))
        keys.append(path.name)
    if document_code:
        keys.append(document_code.upper())
    return list(dict.fromkeys(keys))


def load_section3_annotations(annotation_path: str | Path = DEFAULT_ANNOTATION_PATH) -> dict[str, dict[str, object]]:
    path = Path(annotation_path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if isinstance(data, dict):
        return {str(key): value for key, value in data.items() if isinstance(value, dict)}
    return {}


def find_section3_annotation(
    file_path: str | Path | None,
    document_code: str = "",
    annotation_path: str | Path = DEFAULT_ANNOTATION_PATH,
) -> dict[str, object] | None:
    annotations = load_section3_annotations(annotation_path)
    for key in annotation_keys(file_path, document_code):
        if key in annotations:
            return annotations[key]
    return None


def annotation_range(sections: list[dict[str, object]], annotation: dict[str, object]) -> dict[str, object] | None:
    start_id = str(annotation.get("start_section_id", ""))
    end_id = str(annotation.get("end_section_id", ""))
    section_indexes = {str(section.get("section_id", "")): index for index, section in enumerate(sections)}
    if start_id not in section_indexes or end_id not in section_indexes:
        return None

    start_index = section_indexes[start_id]
    end_index = section_indexes[end_id]
    if end_index < start_index:
        start_index, end_index = end_index, start_index
        start_id, end_id = end_id, start_id
    return {
        "start_index": start_index,
        "end_index": end_index + 1,
        "start_section_id": start_id,
        "end_section_id": end_id,
        "source": "manual_annotation",
        "annotation_stale": False,
    }


def auto_reference_range(
    sections: list[dict[str, object]],
    main_sections: list[dict[str, str]],
    prefer_numbered: bool,
) -> dict[str, object] | None:
    for item in main_sections:
        title = item.get("title", "").casefold()
        number = item.get("number", "")
        if not title.startswith("reference"):
            continue
        if prefer_numbered and not str(number).startswith("3"):
            continue
        start_index = int(item["start_index"])
        end_index = next_heading_index(main_sections, start_index, len(sections))
        return {
            "start_index": start_index,
            "end_index": end_index,
            "start_section_id": str(sections[start_index].get("section_id", "")) if start_index < len(sections) else "",
            "end_section_id": str(sections[end_index - 1].get("section_id", "")) if end_index > start_index else "",
            "source": "auto_numbered_reference" if prefer_numbered else "auto_reference_heading",
            "annotation_stale": False,
        }
    return None


def next_heading_index(main_sections: list[dict[str, str]], start_index: int, default_end: int) -> int:
    for item in main_sections:
        candidate = int(item["start_index"])
        if candidate > start_index:
            return candidate
    return default_end


def empty_reference_range(annotation_stale: bool = False) -> dict[str, object]:
    return {
        "start_index": None,
        "end_index": None,
        "start_section_id": "",
        "end_section_id": "",
        "source": "not_found",
        "annotation_stale": annotation_stale,
    }
