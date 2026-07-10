from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from heapq import heappop, heappush
from typing import Any

from .reference_graph import extract_internal_codes, normalize_controlled_code


EXTERNAL_FIELDS = (
    "reference_regulations",
    "reference_standards",
    "reference_guidance",
    "external_references",
)
INTERNAL_FIELDS = (
    "document_relationships",
    "internal_references",
    "reference_documents",
    "reference_forms",
    "unresolved_internal_references",
    "unmatched_reference_files",
)
IDENTIFIER_TOKEN_RE = re.compile(r"[^\W\d_]+|\d+", re.UNICODE)


@dataclass
class AffectedFileNode:
    document_code: str
    document_title: str
    document_type: str
    file_name: str
    file_path: str
    depth: int
    seed_code: str
    direct_match: bool
    relationship_path: list[str] = field(default_factory=list)
    section3_evidence: list[dict[str, str]] = field(default_factory=list)
    matched_aliases: list[str] = field(default_factory=list)

    @property
    def relationship_path_text(self) -> str:
        return " -> ".join(self.relationship_path)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["relationship_path_text"] = self.relationship_path_text
        return value


@dataclass
class AffectedFilesResult:
    nodes: list[AffectedFileNode] = field(default_factory=list)
    unresolved_references: list[dict[str, Any]] = field(default_factory=list)
    direct_seed_codes: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)

    @property
    def unresolved_codes(self) -> list[str]:
        codes = {str(row.get("referenced_code", "")) for row in self.unresolved_references}
        return sorted((code for code in codes if code), key=str.casefold)

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [node.to_dict() for node in self.nodes],
            "unresolved_references": self.unresolved_references,
            "unresolved_codes": self.unresolved_codes,
            "direct_seed_codes": self.direct_seed_codes,
            "aliases": self.aliases,
        }


def discover_affected_files(documents: list[dict[str, Any]], aliases: list[str]) -> AffectedFilesResult:
    """Find Section 3 alias matches, then follow their cited files forward."""

    alias_specs = _alias_specs(aliases)
    docs_by_key: dict[str, dict[str, Any]] = {}
    for document in sorted(documents, key=_document_sort_key):
        key = _document_key(document)
        if key:
            docs_by_key.setdefault(key, document)

    keys_by_code: dict[str, list[str]] = {}
    for key, document in docs_by_key.items():
        code = _code(document.get("document_code", ""))
        if code:
            keys_by_code.setdefault(code, []).append(key)
    for keys in keys_by_code.values():
        keys.sort(key=str.casefold)

    states: dict[str, dict[str, Any]] = {}
    queue: list[tuple[tuple[Any, ...], str]] = []
    for key, document in docs_by_key.items():
        evidence, matched_aliases = _direct_evidence(document, alias_specs)
        if not evidence:
            continue
        seed_code = _code(document.get("document_code", "")) or _file_name(document) or key
        state = {
            "depth": 0,
            "seed_code": seed_code,
            "path": [seed_code],
            "identity_path": [key],
            "evidence": evidence,
            "matched_aliases": matched_aliases,
        }
        states[key] = state
        heappush(queue, (_state_rank(state), key))

    while queue:
        rank, source_key = heappop(queue)
        state = states.get(source_key)
        if state is None or rank != _state_rank(state):
            continue
        source = docs_by_key[source_key]
        source_code = _code(source.get("document_code", ""))
        for reference_code, raw_evidence in _internal_references(source):
            for target_key in keys_by_code.get(reference_code, []):
                if target_key in state["identity_path"]:
                    continue
                target_code = _code(docs_by_key[target_key].get("document_code", "")) or reference_code
                edge_evidence = {
                    "source_document_code": source_code,
                    "evidence_type": "internal_document_reference",
                    "reference_field": "section_3_internal_reference",
                    "reference_value": raw_evidence,
                    "matched_alias": "",
                    "referenced_code": target_code,
                }
                evidence = state["evidence"] + ([] if edge_evidence in state["evidence"] else [edge_evidence])
                candidate = {
                    "depth": state["depth"] + 1,
                    "seed_code": state["seed_code"],
                    "path": state["path"] + [target_code],
                    "identity_path": state["identity_path"] + [target_key],
                    "evidence": evidence,
                    "matched_aliases": state["matched_aliases"],
                }
                current = states.get(target_key)
                if current is not None and _state_rank(current) <= _state_rank(candidate):
                    continue
                states[target_key] = candidate
                heappush(queue, (_state_rank(candidate), target_key))

    nodes = [_make_node(docs_by_key[key], state) for key, state in states.items()]
    nodes.sort(key=_node_sort_key)
    unresolved = _collect_unresolved(states, docs_by_key, keys_by_code)
    return AffectedFilesResult(
        nodes=nodes,
        unresolved_references=unresolved,
        direct_seed_codes=sorted({node.document_code for node in nodes if node.direct_match}, key=str.casefold),
        aliases=[alias for alias, _ in alias_specs],
    )


def _alias_specs(aliases: list[str]) -> list[tuple[str, tuple[str, ...]]]:
    by_tokens: dict[tuple[str, ...], str] = {}
    for value in aliases:
        alias = unicodedata.normalize("NFKC", str(value or "")).strip()
        tokens = _tokens(alias)
        current = by_tokens.get(tokens)
        if tokens and (current is None or alias.casefold() < current.casefold()):
            by_tokens[tokens] = alias
    ordered = sorted(by_tokens.items(), key=lambda item: (-len(item[0]), item[1].casefold()))
    return [(alias, tokens) for tokens, alias in ordered]


def _tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return tuple(match.group(0) for match in IDENTIFIER_TOKEN_RE.finditer(normalized))


def _identifier_match(reference: str, alias_tokens: tuple[str, ...]) -> bool:
    normalized = unicodedata.normalize("NFKC", str(reference or "")).casefold()
    matches = list(IDENTIFIER_TOKEN_RE.finditer(normalized))
    tokens = tuple(match.group(0) for match in matches)
    width = len(alias_tokens)
    for index in range(len(tokens) - width + 1):
        if tokens[index : index + width] != alias_tokens:
            continue
        end = index + width - 1
        touches_left = index > 0 and matches[index - 1].end() == matches[index].start()
        touches_right = end + 1 < len(matches) and matches[end].end() == matches[end + 1].start()
        if not touches_left and not touches_right:
            return True
    return False


def _direct_evidence(
    document: dict[str, Any],
    alias_specs: list[tuple[str, tuple[str, ...]]],
) -> tuple[list[dict[str, str]], list[str]]:
    if document.get("section3_found") is False:
        return [], []
    code = _code(document.get("document_code", ""))
    evidence: list[dict[str, str]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    matched_aliases: set[str] = set()
    for field_name in EXTERNAL_FIELDS:
        for reference in _values(document.get(field_name)):
            for alias, alias_tokens in alias_specs:
                dedupe_key = (reference.casefold(), alias_tokens)
                if dedupe_key in seen or not _identifier_match(reference, alias_tokens):
                    continue
                seen.add(dedupe_key)
                matched_aliases.add(alias)
                evidence.append(
                    {
                        "source_document_code": code,
                        "evidence_type": "external_alias_match",
                        "reference_field": field_name,
                        "reference_value": reference,
                        "matched_alias": alias,
                        "referenced_code": "",
                    }
                )
    return evidence, sorted(matched_aliases, key=str.casefold)


def _internal_references(document: dict[str, Any]) -> list[tuple[str, str]]:
    if document.get("section3_found") is False:
        return []
    choices: dict[str, set[tuple[int, str]]] = {}

    for item in document.get("reference_items", []) or []:
        if not isinstance(item, dict):
            continue
        item_codes = _codes_in(item.get("reference_code", ""))
        if not item_codes:
            continue
        raw = str(item.get("reference_text") or item.get("raw_reference_text") or item_codes[0])
        for code in item_codes:
            choices.setdefault(code, set()).add((0, raw))
        for matched in item.get("matched_files", []) or []:
            if isinstance(matched, dict):
                matched_code = _code(matched.get("document_code", ""))
                if matched_code:
                    choices.setdefault(matched_code, set()).add((0, raw))

    for priority, field_name in enumerate(INTERNAL_FIELDS, start=1):
        for value in _values(document.get(field_name)):
            for code in _codes_in(value):
                choices.setdefault(code, set()).add((priority, value))

    for matched in document.get("matched_reference_files", []) or []:
        if isinstance(matched, dict):
            code = _code(matched.get("document_code", ""))
            if code:
                choices.setdefault(code, set()).add((len(INTERNAL_FIELDS) + 1, code))

    return [
        (code, min(values, key=lambda item: (item[0], item[1].casefold()))[1])
        for code, values in sorted(choices.items(), key=lambda item: item[0].casefold())
    ]


def _collect_unresolved(
    states: dict[str, dict[str, Any]],
    docs_by_key: dict[str, dict[str, Any]],
    keys_by_code: dict[str, list[str]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for key, state in sorted(states.items(), key=lambda item: _state_rank(item[1])):
        document = docs_by_key[key]
        for reference_code, raw in _internal_references(document):
            if reference_code in keys_by_code:
                continue
            dedupe_key = (key, reference_code, raw)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            rows.append(
                {
                    "source_document_code": _code(document.get("document_code", "")),
                    "source_file_path": _file_path(document),
                    "referenced_code": reference_code,
                    "raw_reference_text": raw,
                    "depth": state["depth"],
                    "seed_code": state["seed_code"],
                    "relationship_path": list(state["path"]),
                    "reason": "No file with matching normalized document code in the supplied document index.",
                }
            )
    return rows


def _make_node(document: dict[str, Any], state: dict[str, Any]) -> AffectedFileNode:
    return AffectedFileNode(
        document_code=_code(document.get("document_code", "")),
        document_title=str(document.get("document_title", "")),
        document_type=str(document.get("document_type", "")),
        file_name=_file_name(document),
        file_path=_file_path(document),
        depth=state["depth"],
        seed_code=state["seed_code"],
        direct_match=state["depth"] == 0,
        relationship_path=list(state["path"]),
        section3_evidence=list(state["evidence"]),
        matched_aliases=list(state["matched_aliases"]),
    )


def _state_rank(state: dict[str, Any]) -> tuple[Any, ...]:
    return (
        state["depth"],
        tuple(value.casefold() for value in state["path"]),
        state["seed_code"].casefold(),
        tuple(value.casefold() for value in state["identity_path"]),
    )


def _node_sort_key(node: AffectedFileNode) -> tuple[Any, ...]:
    return (
        node.depth,
        tuple(value.casefold() for value in node.relationship_path),
        node.document_code.casefold(),
        node.file_path.casefold(),
        node.file_name.casefold(),
    )


def _document_sort_key(document: dict[str, Any]) -> tuple[str, ...]:
    fields = EXTERNAL_FIELDS + INTERNAL_FIELDS + ("reference_items", "matched_reference_files")
    signature = json.dumps(
        {name: document.get(name) for name in fields},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return (
        _code(document.get("document_code", "")).casefold(),
        _file_path(document).casefold(),
        _file_name(document).casefold(),
        signature,
    )


def _document_key(document: dict[str, Any]) -> str:
    identity = _file_path(document) or _file_name(document) or _code(document.get("document_code", ""))
    return unicodedata.normalize("NFKC", identity).casefold()


def _code(value: Any) -> str:
    normalized = normalize_controlled_code(str(value or ""))
    if normalized:
        return normalized.upper()
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")).upper())


def _codes_in(value: Any) -> list[str]:
    extracted = [str(item["code"]).upper() for item in extract_internal_codes(str(value or ""))]
    if extracted:
        return list(dict.fromkeys(extracted))
    fallback = _code(value)
    return [fallback] if fallback else []


def _file_name(document: dict[str, Any]) -> str:
    return str(document.get("source_file_name") or document.get("file_name") or "")


def _file_path(document: dict[str, Any]) -> str:
    return str(document.get("source_file_path") or document.get("file_path") or "")


def _values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    return sorted({str(item) for item in value if item is not None and str(item).strip()}, key=str.casefold)
