from __future__ import annotations

import re
from typing import Any

import pandas as pd

from .regulation_normalizer import alias_regex, aliases_for


def build_reference_tree(updated_regulations: pd.DataFrame, documents: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    regulation_roots = regulation_index(updated_regulations)
    internal_roots: dict[str, dict[str, Any]] = {}
    unknown_root: dict[str, dict[str, Any]] = {}

    for document in documents:
        source = document_node(document)
        for value in external_reference_values(document):
            matched_key = match_regulation(value, regulation_roots)
            if matched_key:
                add_unique_child(regulation_roots[matched_key]["children"], source)
            else:
                add_unknown_reference(unknown_root, value, source)

        reference_items = [item for item in document.get("reference_items", []) if str(item.get("reference_code", "")).strip()]
        for item in reference_items:
            code = str(item.get("reference_code", ""))
            matched_files = item.get("matched_files", []) or []
            if matched_files:
                root = internal_roots.setdefault(source["key"], {**source, "children": []})
                for matched in matched_files:
                    add_unique_child(root["children"], matched_document_node(matched, code))
            else:
                add_unknown_reference(unknown_root, code, source)

    return {
        "regulations": [root for root in regulation_roots.values() if root["children"]],
        "controlled_documents": [root for root in internal_roots.values() if root["children"]],
        "unknown": [root for root in unknown_root.values() if root["children"]],
    }


def regulation_index(updated_regulations: pd.DataFrame) -> dict[str, dict[str, Any]]:
    roots: dict[str, dict[str, Any]] = {}
    if updated_regulations.empty:
        return roots
    for reg_index, reg in updated_regulations.reset_index(drop=True).iterrows():
        old = str(reg.get("原标准编号", "")).strip()
        latest = str(reg.get("最新版本", "")).strip()
        label = old if not latest or latest == old else f"{old} -> {latest}"
        key = f"reg:{reg_index}:{old}:{latest}"
        aliases = [item["alias"] for item in aliases_for(old, latest)]
        roots[key] = {
            "key": key,
            "label": label or f"Regulation row {reg_index + 1}",
            "old_standard": old,
            "latest_standard": latest,
            "status": str(reg.get("状态", "")),
            "aliases": aliases,
            "children": [],
        }
    return roots


def match_regulation(reference: str, roots: dict[str, dict[str, Any]]) -> str:
    text = str(reference or "")
    for key, root in roots.items():
        for alias in root.get("aliases", []):
            if alias and alias_regex(str(alias)).search(text):
                return key
        for value in [root.get("old_standard", ""), root.get("latest_standard", "")]:
            if value and loose_reference_match(str(value), text):
                return key
    return ""


def external_reference_values(document: dict[str, Any]) -> list[str]:
    values = []
    for field in ["reference_regulations", "reference_standards", "reference_guidance"]:
        values.extend(str(item) for item in document.get(field, []) if str(item).strip())
    return list(dict.fromkeys(values))


def loose_reference_match(value: str, reference: str) -> bool:
    normalized_value = re.sub(r"[^A-Z0-9]+", "", value.upper())
    normalized_reference = re.sub(r"[^A-Z0-9]+", "", reference.upper())
    return bool(normalized_value and normalized_value in normalized_reference)


def document_node(document: dict[str, Any]) -> dict[str, str]:
    code = str(document.get("document_code", ""))
    file_name = str(document.get("source_file_name") or document.get("file_name", ""))
    title = str(document.get("document_title", ""))
    label = " | ".join(item for item in [code, title or file_name] if item)
    return {
        "key": str(document.get("source_file_path") or document.get("file_path") or file_name),
        "label": label or file_name,
        "document_code": code,
        "file_name": file_name,
        "file_path": str(document.get("source_file_path") or document.get("file_path", "")),
    }


def matched_document_node(matched: dict[str, Any], fallback_code: str) -> dict[str, str]:
    code = str(matched.get("document_code") or fallback_code)
    file_name = str(matched.get("file_name", ""))
    return {
        "key": str(matched.get("file_path") or file_name or code),
        "label": " | ".join(item for item in [code, file_name] if item),
        "document_code": code,
        "file_name": file_name,
        "file_path": str(matched.get("file_path", "")),
    }


def add_unknown_reference(root: dict[str, dict[str, Any]], reference: str, source: dict[str, str]) -> None:
    key = f"unknown:{reference}"
    node = root.setdefault(key, {"key": key, "label": str(reference), "children": []})
    add_unique_child(node["children"], source)


def add_unique_child(children: list[dict[str, Any]], child: dict[str, Any]) -> None:
    if any(item.get("key") == child.get("key") for item in children):
        return
    children.append(child)
