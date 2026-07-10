from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from modules.document_converter import convert_documents
from modules.document_extractor import (
    DEFAULT_ANNOTATION_PATH,
    annotation_keys,
    chinese_only_markdown,
    document_code_from_filename,
    document_type_from_code,
    extract_chinese_only_blocks,
    extract_document,
    load_section3_annotations,
)


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DEFAULT_DOC_DIR = APP_DIR.parent / "original file"
SUPPORTED_SUFFIXES = {".doc", ".docx", ".xlsx", ".xls"}


st.set_page_config(page_title="Section 3 Annotation", layout="wide")


def discover_files(doc_dir: Path) -> list[Path]:
    if not doc_dir.exists() or not doc_dir.is_dir():
        return []
    return sorted(
        path
        for path in doc_dir.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES and not path.name.startswith("~$")
    )


def extraction_file(file_path: Path) -> Path:
    if file_path.suffix.lower() != ".doc":
        return file_path
    output_dir = DATA_DIR / "section3_annotation_converted"
    conversion = convert_documents(file_path.parent, output_dir)
    for item in conversion["converted_files"]:
        converted = Path(item["file_path"])
        if str(item.get("source_file", "")) == str(file_path) or converted.stem == file_path.stem:
            return converted
    for item in conversion["failed_files"]:
        if str(item.get("file_path", "")) == str(file_path):
            raise RuntimeError(f"Could not convert legacy .doc file: {item.get('reason', '')}")
    converted = output_dir / f"{file_path.stem}.docx"
    if converted.exists():
        return converted
    raise RuntimeError("Could not find converted .docx output for the selected legacy .doc file.")


def save_annotations(records: dict[str, dict[str, object]]) -> None:
    DEFAULT_ANNOTATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_ANNOTATION_PATH.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def current_annotation_key(file_path: Path, document_code: str) -> str:
    return annotation_keys(file_path, document_code)[0]


def annotation_for_file(file_path: Path, document_code: str) -> tuple[str, dict[str, object] | None]:
    records = load_section3_annotations()
    for key in annotation_keys(file_path, document_code):
        if key in records:
            return key, records[key]
    return current_annotation_key(file_path, document_code), None


def section_options(sections: list[dict[str, object]]) -> list[str]:
    return [str(section.get("section_id", "")) for section in sections]


def option_index(options: list[str], value: str) -> int:
    return options.index(value) if value in options else 0


def section_in_range(section_id: str, start_id: str, end_id: str, options: list[str]) -> bool:
    if start_id not in options or end_id not in options or section_id not in options:
        return False
    start = options.index(start_id)
    end = options.index(end_id)
    current = options.index(section_id)
    if end < start:
        start, end = end, start
    return start <= current <= end


def selected_text(sections: list[dict[str, object]], start_id: str, end_id: str) -> str:
    options = section_options(sections)
    return "\n".join(
        str(section.get("text", ""))
        for section in sections
        if section_in_range(str(section.get("section_id", "")), start_id, end_id, options)
    )


def selected_chinese_text(blocks: list[dict[str, object]], start_id: str, end_id: str, options: list[str]) -> str:
    return "\n".join(
        str(block.get("chinese_only_text", ""))
        for block in blocks
        if section_in_range(str(block.get("section_id", "")), start_id, end_id, options)
    )


def render_sections(sections: list[dict[str, object]], start_id: str, end_id: str) -> None:
    options = section_options(sections)
    for section in sections:
        section_id = str(section.get("section_id", ""))
        location = html.escape(str(section.get("page_or_location", "")))
        text = html.escape(str(section.get("text", "")))
        selected = section_in_range(section_id, start_id, end_id, options)
        background = "#15395b" if selected else "#15181f"
        border = "#4aa3ff" if selected else "#303642"
        st.markdown(
            f"""
            <div style="border:1px solid {border}; background:{background}; border-radius:6px;
                        padding:10px 12px; margin:6px 0;">
              <div style="font-size:12px; color:#aeb8c4;">{html.escape(section_id)} · {location}</div>
              <div style="white-space:pre-wrap; color:#f2f5f8;">{text}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )


st.title("Section 3 Annotation")
st.caption("Review and correct the extracted Section 3/reference range for controlled documents.")

doc_dir_text = st.text_input("Controlled-document folder path", value=str(DEFAULT_DOC_DIR))
doc_dir = Path(doc_dir_text).expanduser()
files = discover_files(doc_dir)

if not files:
    st.warning("No .docx or .xlsx controlled documents found in this folder.")
    st.stop()

st.session_state.setdefault("section3_file_index", 0)
st.session_state.section3_file_index = min(st.session_state.section3_file_index, len(files) - 1)

nav_left, nav_mid, nav_right = st.columns([1, 2, 1])
with nav_left:
    if st.button("Previous", disabled=st.session_state.section3_file_index <= 0):
        st.session_state.section3_file_index -= 1
with nav_mid:
    selected_file = st.selectbox(
        "Document",
        files,
        index=st.session_state.section3_file_index,
        format_func=lambda path: path.name,
    )
    st.session_state.section3_file_index = files.index(selected_file)
with nav_right:
    if st.button("Next", disabled=st.session_state.section3_file_index >= len(files) - 1):
        st.session_state.section3_file_index += 1

current_file = files[st.session_state.section3_file_index]
try:
    source_file = extraction_file(current_file)
    document = extract_document(source_file)
except Exception as exc:
    st.error(f"Could not extract selected file: {exc}")
    st.stop()
sections = document["sections"]
options = section_options(sections)
chinese_blocks = extract_chinese_only_blocks(document)
document_code = str(document.get("document_code") or document_code_from_filename(source_file.name))
annotation_key, saved_annotation = annotation_for_file(current_file, document_code)

st.write(f"File {st.session_state.section3_file_index + 1} of {len(files)}")
st.write(
    {
        "document_code": document_code,
        "document_type": document_type_from_code(document_code),
        "extracted_file": source_file.name,
        "detection_source": document.get("reference_detection_source", ""),
        "annotation_stale": document.get("reference_annotation_stale", False),
    }
)
if document.get("reference_annotation_stale"):
    st.warning("Saved annotation section IDs were not found in this file. Automatic detection is shown instead.")

if document.get("reference_document_marker_found"):
    marker = str(document.get("reference_document_marker", ""))
    st.success(f"Found reference marker: {marker}")
    st.text_area(
        "Content after reference marker",
        value=str(document.get("marker_reference_text", "")),
        height=120,
        disabled=True,
    )
else:
    st.info("Exact marker not found. Showing the current Section 3/reference range instead.")

matched_files = document.get("matched_reference_files", []) or []
unmatched_files = document.get("unmatched_reference_files", []) or []
match_col, unmatched_col = st.columns(2)
with match_col:
    st.write("Matched original files")
    if matched_files:
        st.dataframe(matched_files, use_container_width=True)
    else:
        st.caption("No matched original files.")
with unmatched_col:
    st.write("Unmatched extracted references")
    if unmatched_files:
        st.write(unmatched_files)
    else:
        st.caption("No unmatched QSP/QM/WI references.")

detected_start = str(document.get("reference_section_start_id", "")) or (options[0] if options else "")
detected_end = str(document.get("reference_section_end_id", "")) or detected_start
saved_start = str(saved_annotation.get("start_section_id", "")) if saved_annotation else detected_start
saved_end = str(saved_annotation.get("end_section_id", "")) if saved_annotation else detected_end

left, right = st.columns(2)
with left:
    start_id = st.selectbox("Section 3 start section", options, index=option_index(options, saved_start))
with right:
    end_id = st.selectbox("Section 3 end section", options, index=option_index(options, saved_end))

annotated_text = selected_text(sections, start_id, end_id)
annotated_chinese_text = selected_chinese_text(chinese_blocks, start_id, end_id, options)
raw_col, chinese_col = st.columns(2)
with raw_col:
    st.text_area("Selected Section 3/reference text", value=annotated_text, height=220, disabled=True)
with chinese_col:
    st.text_area("Chinese-only Section 3/reference text", value=annotated_chinese_text, height=220, disabled=True)

if st.button("Save Section 3 annotation", type="primary"):
    records = load_section3_annotations()
    record = {
        "file_path": str(current_file.resolve()),
        "extracted_file_path": str(source_file.resolve()),
        "file_name": current_file.name,
        "document_code": document_code,
        "start_section_id": start_id,
        "end_section_id": end_id,
        "annotated_reference_text": annotated_text,
        "annotated_chinese_reference_text": annotated_chinese_text,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    for key in annotation_keys(current_file, document_code) + annotation_keys(source_file, document_code):
        records[key] = record
    save_annotations(records)
    st.success(f"Saved annotation for {current_file.name}")

export_col1, export_col2 = st.columns(2)
with export_col1:
    st.download_button(
        "Download chinese_only_extraction.md",
        chinese_only_markdown(document, chinese_blocks).encode("utf-8"),
        file_name="chinese_only_extraction.md",
        mime="text/markdown",
    )
with export_col2:
    st.download_button(
        "Download chinese_only_extraction.json",
        json.dumps(chinese_blocks, ensure_ascii=False, indent=2).encode("utf-8"),
        file_name="chinese_only_extraction.json",
        mime="application/json",
    )

st.subheader("Document sections")
render_sections(sections, start_id, end_id)

st.subheader("Chinese-only extracted blocks")
for block in chinese_blocks:
    section_id = str(block.get("section_id", ""))
    selected = section_in_range(section_id, start_id, end_id, options)
    if selected:
        st.info(f"{section_id}: {block.get('chinese_only_text', '')}")
    else:
        st.write(f"{section_id}: {block.get('chinese_only_text', '')}")
