from __future__ import annotations

import csv
import json
import re
import unicodedata
from pathlib import Path
from typing import Any


REFERENCE_EXTRACTOR_VERSION = "deterministic-reference-items-v5"
GENERIC_TOPIC_WORDS = {
    "document",
    "record",
    "control",
    "quality",
    "procedure",
    "process",
    "requirement",
    "management",
    "review",
    "change",
    "update",
    "standard",
    "regulation",
    "form",
}

QSP_RE = re.compile(r"(?<![A-Z0-9])QSP\s*[-_]?\s*(\d{4})(?![A-Z0-9])", re.IGNORECASE)
QM_RE = re.compile(r"(?<![A-Z0-9])QM\s*[-_]?\s*(\d{1,3})(?![A-Z0-9])", re.IGNORECASE)
WI_RE = re.compile(r"(?<![A-Z0-9])WI\s*[-_]?\s*(\d{2})\s*[-_]?\s*([A-Z]{2,5})\s*[-_]?\s*(\d{2,4}|XX)(?![A-Z0-9])", re.IGNORECASE)
FM_RE = re.compile(r"(?<![A-Z0-9])FM\s*[-_]?\s*(\d{3,4}[A-Z]?)(?![A-Z0-9])", re.IGNORECASE)
OTHER_INTERNAL_RE = re.compile(r"(?<![A-Z0-9])(?:SOP|CP|FORM)\s*[-_]?\s*(\d{3,4}[A-Z]?)(?![A-Z0-9])", re.IGNORECASE)
BODY_START_RE = re.compile(r"(?:^|\s)1(?:\.0)?\s*[\.、]?\s*(?:Purpose|目的)\b", re.IGNORECASE)
REFERENCE_HEADING_PATTERN = (
    r"(?:"
    r"(?:参考\s*文件|參考\s*文件)(?:\s*references?\s*(?:documents?|doc\.?|docs?)?)?"
    r"|references?\s+documents?"
    r"|references?"
    r"|reference\s+\d{1,3}"
    r")"
)
SECTION3_HEADING_RE = re.compile(
    rf"^\s*(?:[•·\-]\s*)?(?:3(?:\.0)?\s*[\.、]?\s*)?{REFERENCE_HEADING_PATTERN}(?:\s+\d{{1,3}})?\s*$",
    re.IGNORECASE,
)
NUMBERED_REFERENCE_HEADING_RE = re.compile(
    rf"^\s*(?:[•·\-]\s*)?3(?:\.0)?\s*[\.、]?\s*{REFERENCE_HEADING_PATTERN}(?:\s+\d{{1,3}})?\s*$",
    re.IGNORECASE,
)
NUMBERED_REFERENCE_PREFIX_RE = re.compile(
    rf"^\s*(?:[•·\-]\s*)?3(?:\.0)?\s*[\.、]?\s*{REFERENCE_HEADING_PATTERN}(?=\s|$)",
    re.IGNORECASE,
)
NEXT_MAIN_HEADING_RE = re.compile(
    r"^\s*(?:[•·\-]\s*)?[4-9](?:\.0)?\s*[\.、]?\s*(?:定义|定義|definition|responsibilit(?:y|ies)|职责|職責|procedure|程序|作业内容|records?|forms?|\S+)?\b",
    re.IGNORECASE,
)
INLINE_NEXT_MAIN_HEADING_RE = re.compile(
    r"(?:^|\s)[4-9](?:\.0)?\s*[\.、]?\s*(?:定义|定義|definition|responsibilit(?:y|ies)|职责|職責|procedure|程序|作业内容|records?|forms?|\S+)?\b",
    re.IGNORECASE,
)
REFERENCE_MARKER_RE = re.compile(r"(參考文件|参考文件)(?:\s*References?(?:\s+Document)?|\s*Reference\s+Document)?", re.IGNORECASE)
BAD_REFERENCE_HEADING_CONTEXT_RE = re.compile(
    r"\b(?:add(?:ed)?|updat(?:ed|e)?|modif(?:ied|y)|chang(?:ed|e)?|delet(?:ed|e)?|section|clause|table|matrix|history)\b"
    r"|新增|增加|更改|修改|删除|刪除|更改历史|更改歷史|目錄|目录",
    re.IGNORECASE,
)
REFERENCE_ITEM_STOP_RE = re.compile(
    r"^\s*(?:[•·\-]\s*)?(?:\d+(?:\.\d+)*\s*)?(?:定义|定義|definition|职责|職責|responsibilit(?:y|ies)|程序|procedure|作业内容|records?|forms?)\b",
    re.IGNORECASE,
)
REFERENCE_BLOCK_STOP_RE = re.compile(
    r"^\s*(?:[•·\-]\s*)?(?:\d+(?:\.\d+)*\s*)?"
    r"(?:定义|定義|definition|职责|職責|responsibilit(?:y|ies)|程序|procedure|内容|content|附件|appendix|记录|record|目的|purpose|范围|scope|作业内容)\b",
    re.IGNORECASE,
)
NO_REFERENCE_RE = re.compile(r"^\s*(?:\d+\.)?\s*(?:n/?a|无|無|nil|none)\s*$", re.IGNORECASE)
EXTERNAL_REFERENCE_RE = re.compile(
    r"(?:BS\s+EN\s+)?(?:EN\s+)?ISO\s*\d+(?:-\d+)?(?::\d{4})?"
    r"|IEC\s*\d+(?::\d{4})?"
    r"|ASTM\s*F\s*\d+"
    r"|AAMI\s*TIR\s*\d+"
    r"|Regulation\s*\(?EU\)?\s*2017/745"
    r"|(?:EU\s+)?MDR"
    r"|MDCG\s*\d{4}-\d+"
    r"|GB/T\s*\d+(?:\.\d+)?"
    r"|YY/T\s*\d+(?:\.\d+)?"
    r"|JJG\s*\d+|JJF\s*\d+"
    r"|GN-\d+|COP|TR\s*\d+",
    re.IGNORECASE,
)


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).replace("：", ":")


def normalize_controlled_code(value: str) -> str:
    text = normalize_text(value).upper()
    for pattern, formatter in [
        (WI_RE, lambda m: f"WI-{m.group(1)}-{m.group(2).upper()}-{m.group(3)}"),
        (QSP_RE, lambda m: f"QSP{m.group(1)}"),
        (QM_RE, lambda m: f"QM{int(m.group(1)):02d}" if len(m.group(1)) <= 2 else f"QM{m.group(1)}"),
        (FM_RE, lambda m: f"FM{m.group(1).upper()}"),
        (OTHER_INTERNAL_RE, lambda m: f"{m.group(0)[: m.group(0).upper().find(m.group(1))].strip().replace('-', '').replace('_', '')}{m.group(1).upper()}"),
    ]:
        match = pattern.search(text)
        if match:
            return formatter(match)
    return ""


def document_code_from_filename(file_name: str) -> str:
    stem = Path(file_name).stem
    code = normalize_controlled_code(stem)
    return code or stem.split()[0]


def document_type_from_code(code: str) -> str:
    upper = str(code).upper()
    for prefix in ["QM", "QSP", "WI", "FM", "SOP", "CP", "FORM"]:
        if upper.startswith(prefix):
            return prefix
    return "Other"


def extract_internal_codes(text: str) -> list[dict[str, str]]:
    normalized = normalize_text(text)
    matches: list[tuple[int, str, str]] = []
    for pattern in [WI_RE, QSP_RE, QM_RE, FM_RE, OTHER_INTERNAL_RE]:
        for match in pattern.finditer(normalized):
            code = normalize_controlled_code(match.group(0))
            if code:
                matches.append((match.start(), code, match.group(0)))
    result = []
    seen = set()
    for _, code, raw in sorted(matches, key=lambda item: item[0]):
        if code in seen:
            continue
        seen.add(code)
        result.append({"code": code, "raw": raw, "confidence": "high"})
    return result


def clean_reference_lines(text: str) -> list[str]:
    lines = []
    for raw_line in str(text or "").replace("\u2028", "\n").splitlines():
        line = raw_line.replace("\u3000", " ")
        line = re.sub(r"\s+", " ", line).strip(" \t\r\n:-：")
        if not line or "EMBED " in line:
            continue
        lines.append(line)
    return lines


def plain_text_reference_range(text: str) -> dict[str, Any]:
    lines = clean_reference_lines(text)
    candidates = []
    for index, line in enumerate(lines):
        match = full_text_reference_heading_match(line)
        if not match:
            continue
        block = []
        tail = line[match.end() :].strip(" -:：|;；,，")
        if tail:
            inline_end = INLINE_NEXT_MAIN_HEADING_RE.search(tail)
            block.append(tail[: inline_end.start()].strip() if inline_end else tail)
        for candidate in lines[index + 1 :]:
            if REFERENCE_BLOCK_STOP_RE.search(candidate) or NEXT_MAIN_HEADING_RE.search(candidate):
                break
            block.append(candidate)
            if len(block) >= 120:
                break
        score = reference_block_score(line, block)
        candidates.append((score, index, block, line))

    candidates = [candidate for candidate in candidates if candidate[0] > 0]
    if not candidates:
        return {
            "source": "not_found",
            "reference_text": "",
            "reference_marker": "",
            "section3_found": False,
        }
    candidates.sort(key=lambda item: (-item[0], item[1]))
    _, _, block, marker = candidates[0]
    return {
        "source": "plain_text_reference_block",
        "reference_text": "\n".join(item for item in block if item.strip()),
        "reference_marker": marker,
        "section3_found": True,
    }


def full_text_reference_heading_match(line: str) -> re.Match[str] | None:
    normalized = normalize_text(line).strip()
    match = SECTION3_HEADING_RE.search(normalized)
    if not match:
        return None
    if len(normalized) > 120 and not extract_internal_codes(normalized):
        return None
    if BAD_REFERENCE_HEADING_CONTEXT_RE.search(normalized):
        return None
    if extract_internal_codes(normalized) and not re.search(r"^(?:[•·\-]\s*)?(?:\d+(?:\.\d+)*\s*)?(?:参考|參考|references?)", normalized, re.I):
        return None
    return match


def reference_block_score(marker: str, block: list[str]) -> int:
    score = 0
    score += sum(3 for item in block if extract_internal_codes(item))
    score += sum(2 for item in block if NO_REFERENCE_RE.match(item))
    if block and all(re.search(r"^(\d+(?:\.\d+)*|[A-Z]\.?)$", item) for item in block[:3]):
        score -= 5
    if re.search(r"\s\d{1,3}$", marker) and not any(extract_internal_codes(item) for item in block[:8]):
        score -= 3
    if re.search(r"^\s*(?:[•·\-]\s*)?3(?:\.0)?\b", marker):
        score += 2
    if REFERENCE_MARKER_RE.search(marker):
        score += 2
    return score


def extract_reference_items(text: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    current: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal current
        if not current:
            return
        reference_name = " ".join(current["name_parts"]).strip(" ;,，")
        reference_text = " ".join(current["text_parts"]).strip(" ;,，")
        items.append(
            {
                "reference_code": str(current["code"]),
                "reference_name": reference_name,
                "reference_text": reference_text,
            }
        )
        current = None

    for line in clean_reference_lines(text):
        if REFERENCE_ITEM_STOP_RE.search(line):
            break
        if SECTION3_HEADING_RE.search(line) and not extract_internal_codes(line):
            continue
        if NO_REFERENCE_RE.match(line):
            flush()
            items.append({"reference_code": "", "reference_name": "N/A", "reference_text": line})
            continue

        matches = list(_internal_code_matches(line))
        if matches:
            for index, match in enumerate(matches):
                start = match.start()
                end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
                segment = line[start:end].strip(" ;,，")
                code = normalize_controlled_code(match.group(0))
                name = segment[match.end() - start :].strip(" -:：|;；,，")
                flush()
                current = {"code": code, "name_parts": [], "text_parts": [segment]}
                if name:
                    current["name_parts"].append(name)
            prefix = line[: matches[0].start()].strip(" ;,，")
            if prefix and current and not list(_internal_code_matches(prefix)):
                current["name_parts"].append(prefix)
                current["text_parts"].append(prefix)
        elif current:
            current["name_parts"].append(line)
            current["text_parts"].append(line)
        else:
            items.append({"reference_code": "", "reference_name": line, "reference_text": line})

    flush()

    deduped = []
    seen = set()
    for item in items:
        key = (item["reference_code"], item["reference_name"], item["reference_text"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _internal_code_matches(text: str):
    normalized = normalize_text(text)
    matches = []
    for pattern in [WI_RE, QSP_RE, QM_RE, FM_RE, OTHER_INTERNAL_RE]:
        matches.extend(pattern.finditer(normalized))
    return sorted(matches, key=lambda match: match.start())


def extract_external_references(text: str) -> list[str]:
    values = []
    seen = set()
    for match in EXTERNAL_REFERENCE_RE.finditer(normalize_text(text)):
        value = normalize_external_reference(match.group(0))
        if value not in seen:
            seen.add(value)
            values.append(value)
    return values


def normalize_external_reference(value: str) -> str:
    text = re.sub(r"\s+", " ", normalize_text(value).strip())
    upper = text.upper()
    if re.fullmatch(r"ISO\s+\d+.*", upper):
        return upper.replace("ISO ", "ISO", 1)
    if upper.startswith("REGULATION"):
        return "Regulation (EU) 2017/745"
    return upper


def extract_header_metadata(file_name: str, sections: list[dict[str, object]]) -> dict[str, str]:
    early_text = "\n".join(str(section.get("text", "")) for section in sections[:20])[:3000]
    code = document_code_from_filename(file_name) or normalize_controlled_code(early_text)
    revision = ""
    rev_match = re.search(r"(?:Doc\s*Rev\.?|文件版本)\s*[:：]?\s*([A-Z0-9.\-]+)", early_text, re.IGNORECASE)
    if rev_match:
        revision = rev_match.group(1).strip()
    return {
        "document_code": code,
        "document_type": document_type_from_code(code),
        "document_title": Path(file_name).stem,
        "doc_revision": revision,
    }


def deterministic_reference_range(
    sections: list[dict[str, object]],
    main_sections: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    del main_sections
    body_start = body_start_index(sections)
    start_search = body_start if body_start is not None else 0
    warnings = []
    if body_start is None:
        warnings.append("body_start_not_found")

    candidates = []
    for index in range(start_search, len(sections)):
        text = str(sections[index].get("text", ""))
        match = reference_heading_match(text)
        if not match:
            continue
        reference_parts = [text[match.start() :]]
        end_index = len(sections)
        end_method = "document_end"
        for next_index in range(index + 1, len(sections)):
            next_text = str(sections[next_index].get("text", ""))
            if NEXT_MAIN_HEADING_RE.search(next_text):
                end_index = next_index
                end_method = "next_main_heading"
                break
            reference_parts.append(next_text)
        if end_index == len(sections):
            inline_end = INLINE_NEXT_MAIN_HEADING_RE.search(reference_parts[0], match.end())
            if inline_end:
                reference_parts[0] = reference_parts[0][: inline_end.start()]
                end_index = index + 1
                end_method = "inline_next_main_heading"
        reference_text = "\n".join(part.strip() for part in reference_parts if part.strip())
        if len(reference_text) > 1500 and end_method == "document_end":
            reference_text = reference_text[:1500]
            end_method = "char_limit"
            warnings.append("reference_text_limited")
        score = reference_heading_score(text, reference_text)
        candidates.append(
            (
                -score,
                index,
                {
                    "start_index": index,
                    "end_index": max(end_index, index + 1),
                    "start_section_id": str(sections[index].get("section_id", "")),
                    "end_section_id": str(sections[max(min(end_index, len(sections)) - 1, index)].get("section_id", "")),
                    "source": "deterministic_section3",
                    "reference_text": reference_text,
                    "body_start_found": body_start is not None,
                    "section3_found": True,
                    "section3_start_method": "body_after_section3_heading" if body_start is not None else "global_section3_heading",
                    "section3_end_method": end_method,
                    "extraction_warnings": warnings,
                },
            )
        )
    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[0][2]
    return {
        "start_index": None,
        "end_index": None,
        "start_section_id": "",
        "end_section_id": "",
        "source": "not_found",
        "reference_text": "",
        "body_start_found": body_start is not None,
        "section3_found": False,
        "section3_start_method": "not_found",
        "section3_end_method": "not_found",
        "extraction_warnings": warnings or ["section3_not_found"],
    }


def reference_heading_match(text: str) -> re.Match[str] | None:
    normalized = normalize_text(text).strip()
    if len(normalized) > 120 and not extract_internal_codes(normalized):
        return None
    if BAD_REFERENCE_HEADING_CONTEXT_RE.search(normalized):
        return None
    match = NUMBERED_REFERENCE_HEADING_RE.search(normalized)
    if match:
        return match
    match = NUMBERED_REFERENCE_PREFIX_RE.search(normalized)
    if match and not re.match(r"\s+to\b", normalized[match.end() :], re.IGNORECASE):
        return match
    marker = REFERENCE_MARKER_RE.search(normalized)
    if marker and marker.start() <= 20:
        return marker
    return None


def reference_heading_score(heading_text: str, reference_text: str) -> int:
    score = 0
    heading = normalize_text(heading_text)
    items = extract_reference_items(reference_text)
    score += sum(3 for item in items if item.get("reference_code"))
    score += sum(2 for item in items if str(item.get("reference_name", "")).upper() == "N/A")
    if re.search(r"^\s*(?:[•·\-]\s*)?3(?:\.0)?\b", heading):
        score += 4
    if REFERENCE_MARKER_RE.search(heading):
        score += 2
    if re.search(r"\s\d{1,3}$", heading) and not any(item.get("reference_code") for item in items):
        score -= 3
    return score


def build_reference_map_rows(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for document in documents:
        source_file = str(document.get("source_file_name") or document.get("file_name", ""))
        source_path = str(document.get("source_file_path") or document.get("file_path", ""))
        source_code = str(document.get("document_code", ""))
        reference_items = [item for item in document.get("reference_items", []) if str(item.get("reference_code", ""))]
        if not reference_items:
            rows.append(
                {
                    "source_document_code": source_code,
                    "source_file": source_file,
                    "source_file_path": source_path,
                    "reference_code": "",
                    "reference_name": "",
                    "reference_text": "",
                    "match_status": "no_internal_reference",
                    "matched_document_code": "",
                    "matched_file": "",
                    "matched_file_path": "",
                    "extraction_source": document.get("reference_detection_source", ""),
                }
            )
            continue
        for item in reference_items:
            matched_files = item.get("matched_files", []) or []
            if matched_files:
                for matched in matched_files:
                    rows.append(
                        {
                            "source_document_code": source_code,
                            "source_file": source_file,
                            "source_file_path": source_path,
                            "reference_code": item.get("reference_code", ""),
                            "reference_name": item.get("reference_name", ""),
                            "reference_text": item.get("reference_text", ""),
                            "match_status": item.get("match_status", "matched"),
                            "matched_document_code": matched.get("document_code", ""),
                            "matched_file": matched.get("file_name", ""),
                            "matched_file_path": matched.get("file_path", ""),
                            "extraction_source": document.get("reference_detection_source", ""),
                        }
                    )
            else:
                rows.append(
                    {
                        "source_document_code": source_code,
                        "source_file": source_file,
                        "source_file_path": source_path,
                        "reference_code": item.get("reference_code", ""),
                        "reference_name": item.get("reference_name", ""),
                        "reference_text": item.get("reference_text", ""),
                        "match_status": item.get("match_status", "unresolved"),
                        "matched_document_code": "",
                        "matched_file": "",
                        "matched_file_path": "",
                        "extraction_source": document.get("reference_detection_source", ""),
                    }
                )
    return rows


def body_start_index(sections: list[dict[str, object]]) -> int | None:
    for index, section in enumerate(sections):
        if BODY_START_RE.search(str(section.get("text", ""))):
            return index
    return None


def build_file_index(documents: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    index: dict[str, dict[str, str]] = {}
    for document in documents:
        code = str(document.get("document_code", "")).upper()
        if not code:
            continue
        index[code] = {
            "document_code": code,
            "file_name": str(document.get("source_file_name") or document.get("file_name", "")),
            "file_path": str(document.get("source_file_path") or document.get("file_path", "")),
            "document_type": str(document.get("document_type", "")),
            "document_title": str(document.get("document_title", "")),
        }
    return index


def attach_reference_matches(documents: list[dict[str, Any]]) -> dict[str, Any]:
    file_index = build_file_index(documents)
    forward: dict[str, list[str]] = {}
    reverse: dict[str, list[str]] = {}
    unresolved = []
    for document in documents:
        source = str(document.get("document_code", "")).upper()
        refs = [str(code).upper() for code in document.get("document_relationships", []) if str(code).upper() != source]
        matched = []
        unresolved_codes = []
        for code in dict.fromkeys(refs):
            if code in file_index:
                matched.append(file_index[code])
                reverse.setdefault(code, []).append(source)
            else:
                unresolved_codes.append(code)
                unresolved.append(
                    {
                        "source_document_code": source,
                        "referenced_code": code,
                        "raw_reference_text": code,
                        "source_file_path": str(document.get("file_path", "")),
                        "reason": "No file with matching normalized document code in original file index.",
                    }
                )
        document["matched_reference_files"] = matched
        document["unresolved_internal_references"] = unresolved_codes
        forward[source] = [item["document_code"] for item in matched]
    for code, sources in reverse.items():
        reverse[code] = sorted(set(sources))
    return {"file_index": file_index, "forward": forward, "reverse": reverse, "unresolved": unresolved}


def reference_summary(documents: list[dict[str, Any]], graph: dict[str, Any] | None = None) -> dict[str, int]:
    graph = graph or {}
    return {
        "total_documents": len(documents),
        "documents_with_section3": sum(1 for document in documents if document.get("section3_found")),
        "internal_references": sum(len(document.get("document_relationships", [])) for document in documents),
        "external_references": sum(
            len(document.get("reference_regulations", []))
            + len(document.get("reference_standards", []))
            + len(document.get("reference_guidance", []))
            for document in documents
        ),
        "unresolved_internal_references": len(graph.get("unresolved", [])),
    }


def export_reference_audit(documents: list[dict[str, Any]], graph: dict[str, Any], output_dir: str | Path) -> None:
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    index_rows = []
    log_rows = []
    for document in documents:
        index_rows.append(
            {
                "document_code": document.get("document_code", ""),
                "document_type": document.get("document_type", ""),
                "document_title": document.get("document_title", ""),
                "file_path": document.get("file_path", ""),
                "doc_rev": document.get("doc_revision", ""),
                "section3_found": document.get("section3_found", False),
                "section3_text_preview": str(document.get("reference_text", ""))[:500],
                "internal_references": ", ".join(document.get("document_relationships", [])),
                "external_references": ", ".join(
                    list(document.get("reference_regulations", []))
                    + list(document.get("reference_standards", []))
                    + list(document.get("reference_guidance", []))
                ),
                "unresolved_internal_references": ", ".join(document.get("unresolved_internal_references", [])),
                "extraction_confidence": "high" if document.get("section3_found") else "low",
                "extraction_warnings": "; ".join(document.get("extraction_warnings", [])),
            }
        )
        log_rows.append(
            {
                "document_code": document.get("document_code", ""),
                "file_path": document.get("file_path", ""),
                "body_start_found": document.get("body_start_found", False),
                "section3_found": document.get("section3_found", False),
                "section3_start_method": document.get("section3_start_method", ""),
                "section3_end_method": document.get("section3_end_method", ""),
                "number_internal_refs": len(document.get("document_relationships", [])),
                "number_external_refs": len(document.get("reference_regulations", []))
                + len(document.get("reference_standards", []))
                + len(document.get("reference_guidance", [])),
                "warnings": "; ".join(document.get("extraction_warnings", [])),
            }
        )

    (path / "document_reference_index.json").write_text(json.dumps(index_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (path / "document_relationship_graph.json").write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    relationship_rows = build_reference_map_rows(documents)
    (path / "document_relationship_map.json").write_text(json.dumps(relationship_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(path / "document_reference_index.csv", index_rows)
    write_csv(path / "document_relationship_map.csv", relationship_rows)
    write_csv(path / "unresolved_internal_references.csv", graph.get("unresolved", []))
    write_csv(path / "reference_extraction_log.csv", log_rows)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = list(rows[0].keys()) if rows else ["empty"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
