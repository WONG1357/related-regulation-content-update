from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from .regulation_version_comparator import extract_regulation_text


_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_FILE_SUFFIX_RE = re.compile(r"\.(?:pdf|docx|xlsx|txt)$", re.IGNORECASE)
_COPY_SUFFIX_RE = re.compile(
    r"(?:\s*[\(\[\{]\s*\d{1,3}\s*[\)\]\}]|\s*[-_]?\s*(?:copy|duplicate|副本|拷贝)(?:\s*\d+)?)$",
    re.IGNORECASE,
)


class RegulationIdentityError(ValueError):
    """Raised when a regulation pair cannot be resolved without guessing."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "details": self.details}


@dataclass(frozen=True)
class ParsedRegulationIdentity:
    file_path: str
    file_name: str
    normalized_file_name: str
    family: str
    year: int
    family_source: str
    year_source: str
    detected_families: tuple[str, ...]
    detected_years: tuple[int, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "file_name": self.file_name,
            "normalized_file_name": self.normalized_file_name,
            "family": self.family,
            "year": self.year,
            "family_source": self.family_source,
            "year_source": self.year_source,
            "detected_families": list(self.detected_families),
            "detected_years": list(self.detected_years),
        }


# Kept as a descriptive alias for callers that refer to an uploaded-file identity.
ParsedUploadIdentity = ParsedRegulationIdentity


@dataclass(frozen=True)
class ResolvedRegulationPair:
    old: ParsedRegulationIdentity
    latest: ParsedRegulationIdentity
    family: str
    old_year: int
    latest_year: int
    match_mode: str
    aliases: tuple[str, ...]
    matched_row_numbers: tuple[int, ...]
    matched_rows: tuple[dict[str, Any], ...]
    workbook_path: str
    sheet_name: str
    warnings: tuple[str, ...] = ()

    @property
    def old_file(self) -> str:
        return self.old.file_path

    @property
    def latest_file(self) -> str:
        return self.latest.file_path

    @property
    def regulation_id(self) -> str:
        return self.family

    @property
    def old_version(self) -> str:
        return str(self.old_year)

    @property
    def latest_version(self) -> str:
        return str(self.latest_year)

    def to_dict(self) -> dict[str, Any]:
        return {
            "old": self.old.to_dict(),
            "latest": self.latest.to_dict(),
            "family": self.family,
            "old_year": self.old_year,
            "latest_year": self.latest_year,
            "match_mode": self.match_mode,
            "aliases": list(self.aliases),
            "matched_row_numbers": list(self.matched_row_numbers),
            "matched_rows": [dict(row) for row in self.matched_rows],
            "workbook_path": self.workbook_path,
            "sheet_name": self.sheet_name,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class _WorkbookAliasRow:
    row_number: int
    original_identifier: str
    original_year: int | None
    original_family: str
    latest_identifier: str
    latest_year: int | None
    latest_family: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_number": self.row_number,
            "original_identifier": self.original_identifier,
            "original_year": self.original_year,
            "original_family": self.original_family,
            "latest_identifier": self.latest_identifier,
            "latest_year": self.latest_year,
            "latest_family": self.latest_family,
        }


def resolve_regulation_pair(
    first_file: Path,
    second_file: Path,
    workbook_path: Path,
    sheet_name: str = "原始总表",
) -> ResolvedRegulationPair:
    """Resolve and order two editions against the workbook alias registry."""

    first = parse_upload_identity(first_file)
    second = parse_upload_identity(second_file)
    if first.family != second.family:
        raise RegulationIdentityError(
            "family_mismatch",
            f"Uploaded files resolve to different regulation families: {first.family} and {second.family}.",
            {"first": first.to_dict(), "second": second.to_dict()},
        )
    if first.year == second.year:
        raise RegulationIdentityError(
            "same_edition",
            f"Both uploaded files resolve to {first.family} {first.year}; two different editions are required.",
            {"first": first.to_dict(), "second": second.to_dict()},
        )

    old, latest = sorted((first, second), key=lambda item: item.year)
    rows = _load_alias_rows(workbook_path, sheet_name)
    latest_candidates = [
        row
        for row in rows
        if row.latest_family == latest.family and row.latest_year == latest.year
    ]
    if not latest_candidates:
        raise RegulationIdentityError(
            "latest_not_found",
            f"No numbered workbook row matches {latest.family} {latest.year} as the latest edition.",
            {"family": latest.family, "latest_year": latest.year, "sheet_name": sheet_name},
        )

    exact_candidates = [
        row
        for row in latest_candidates
        if row.original_family == old.family and row.original_year == old.year
    ]
    if exact_candidates:
        selected = exact_candidates
        match_mode = "exact_match"
        warnings: tuple[str, ...] = ()
    else:
        selected = [row for row in latest_candidates if row.original_family == old.family]
        if not selected:
            raise RegulationIdentityError(
                "family_not_found",
                f"Workbook rows for {latest.family} {latest.year} do not belong to the uploaded family.",
                {"candidate_rows": [row.to_dict() for row in latest_candidates]},
            )
        match_mode = "family_match"
        warnings = (
            f"The workbook has no exact {old.family} {old.year} row; the pair was accepted by family and latest year.",
        )

    edition_groups = {
        _latest_edition_group(row.latest_identifier, latest.family)
        for row in selected
    }
    if len(edition_groups) != 1:
        raise RegulationIdentityError(
            "ambiguous_latest_match",
            f"Multiple workbook latest-edition groups match {latest.family} {latest.year}.",
            {
                "edition_groups": sorted(edition_groups),
                "candidate_rows": [row.to_dict() for row in selected],
            },
        )

    aliases = _aggregate_family_aliases(rows, latest.family)
    return ResolvedRegulationPair(
        old=old,
        latest=latest,
        family=latest.family,
        old_year=old.year,
        latest_year=latest.year,
        match_mode=match_mode,
        aliases=aliases,
        matched_row_numbers=tuple(sorted(row.row_number for row in selected)),
        matched_rows=tuple(row.to_dict() for row in sorted(selected, key=lambda item: item.row_number)),
        workbook_path=str(Path(workbook_path).resolve()),
        sheet_name=sheet_name,
        warnings=warnings,
    )


def parse_upload_identity(file_path: Path) -> ParsedRegulationIdentity:
    path = Path(file_path)
    if not path.is_file():
        raise RegulationIdentityError("file_not_found", f"Regulation file does not exist: {path}")

    normalized_name = normalize_identifier(path.name)
    text = extract_regulation_text(path)
    filename_families = _families_in_text(normalized_name)
    content_families = _families_in_text(text[:50000])
    family, family_source = _select_upload_family(filename_families, content_families, text)

    filename_years = _years_in_text(normalized_name)
    if len(filename_years) == 1:
        year = filename_years[0]
        year_source = "filename"
    elif len(filename_years) > 1:
        year = _select_year_for_family(normalized_name, family)
        year_source = "filename"
    else:
        year = _select_year_for_family(text[:50000], family)
        year_source = "content"

    detected_years = tuple(sorted(set(filename_years + _family_years_in_text(text[:50000], family))))
    detected_families = tuple(_dedupe(filename_families + content_families))
    return ParsedRegulationIdentity(
        file_path=str(path.resolve()),
        file_name=path.name,
        normalized_file_name=normalized_name,
        family=family,
        year=year,
        family_source=family_source,
        year_source=year_source,
        detected_families=detected_families,
        detected_years=detected_years,
    )


def normalize_identifier(value: object) -> str:
    """Normalize a filename or regulation identifier for deterministic matching."""

    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = _FILE_SUFFIX_RE.sub("", text)
    text = re.sub(r"^[\s_\-]*SAR[\s_\-]*\d+[\s_\-]*", "", text, flags=re.IGNORECASE)
    while _COPY_SUFFIX_RE.search(text):
        text = _COPY_SUFFIX_RE.sub("", text).strip()
    text = text.translate(
        str.maketrans(
            {
                "‐": "-",
                "‑": "-",
                "‒": "-",
                "–": "-",
                "—": "-",
                "−": "-",
                "：": ":",
                "／": "/",
                "＋": "+",
                "，": ",",
                "（": "(",
                "）": ")",
            }
        )
    )
    text = re.sub(r"\s*([:/+,])\s*", r"\1", text)
    text = re.sub(r"\s*-\s*", "-", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" _-").upper()


def extract_regulation_family(value: object) -> str:
    """Return one unambiguous primary family from an identifier."""

    families = _families_in_text(value)
    iso_families = [family for family in families if family.startswith(("ISO ", "ISO/IEC "))]
    if len(iso_families) == 1:
        return iso_families[0]
    if len(families) == 1:
        return families[0]
    return ""


def _select_upload_family(
    filename_families: list[str],
    content_families: list[str],
    content: str,
) -> tuple[str, str]:
    filename_primary = _preferred_family(filename_families)
    if filename_primary:
        return filename_primary, "filename"

    if not content_families:
        raise RegulationIdentityError(
            "family_missing",
            "No supported regulation family could be identified from the filename or file content.",
        )
    counts = {family: _family_occurrence_count(content, family) for family in set(content_families)}
    highest = max(counts.values())
    leaders = sorted(family for family, count in counts.items() if count == highest)
    if len(leaders) != 1:
        raise RegulationIdentityError(
            "family_ambiguous",
            "File content contains multiple equally likely regulation families.",
            {"candidates": leaders},
        )
    return leaders[0], "content"


def _preferred_family(families: list[str]) -> str:
    unique = _dedupe(families)
    iso = [family for family in unique if family.startswith(("ISO ", "ISO/IEC "))]
    if len(iso) == 1:
        return iso[0]
    if len(unique) == 1:
        return unique[0]
    if unique:
        raise RegulationIdentityError(
            "family_ambiguous",
            "Filename contains multiple regulation families.",
            {"candidates": unique},
        )
    return ""


def _select_year_for_family(value: object, family: str) -> int:
    family_years = _family_years_in_text(value, family)
    if not family_years:
        edition_years = [
            int(match.group(1))
            for match in re.finditer(r"\b(?:edition|version|issued|published)\D{0,20}((?:19|20)\d{2})\b", str(value), re.I)
        ]
        family_years = edition_years
    unique = sorted(set(family_years))
    if len(unique) == 1:
        return unique[0]
    if not unique:
        raise RegulationIdentityError(
            "year_missing",
            f"No edition year could be identified for {family}.",
        )

    normalized = normalize_identifier(value)
    amendment_years = [
        int(match.group(1))
        for match in re.finditer(r"\b(?:AMD|AMENDMENT|A\d+|COR|CORRIGENDUM)\s*[:\-]?\s*((?:19|20)\d{2})\b", normalized)
    ]
    if len(set(amendment_years)) == 1:
        return amendment_years[0]
    raise RegulationIdentityError(
        "year_ambiguous",
        f"Multiple edition years were found for {family}.",
        {"candidates": unique},
    )


def _families_in_text(value: object) -> list[str]:
    text = normalize_identifier(value)
    found: list[tuple[int, int, str]] = []
    patterns: list[tuple[int, re.Pattern[str], str]] = [
        (0, re.compile(r"\bISO\s*/\s*IEC\s*[- ]?\s*(\d{3,6}(?:-\d+)*)\b"), "ISO/IEC"),
        (1, re.compile(r"\bISO\s*[- ]?\s*(\d{3,6}(?:-\d+)*)\b"), "ISO"),
        (2, re.compile(r"\bIEC\s*[- ]?\s*(\d{3,6}(?:-\d+)*)\b"), "IEC"),
        (3, re.compile(r"\bASTM\s*([A-Z])\s*-?\s*(\d{2,6}[A-Z]?)\b"), "ASTM"),
        (4, re.compile(r"\bYY\s*/\s*T\s*[- ]?\s*(\d{3,6}(?:\.\d+)?)\b"), "YY/T"),
        (5, re.compile(r"\bEN\s*[- ]?\s*(\d{3,6}(?:-\d+)*)\b"), "EN"),
        (6, re.compile(r"\bBS\s*[- ]?\s*(\d{3,6}(?:-\d+)*)\b"), "BS"),
    ]
    occupied_iso_iec: list[tuple[int, int]] = []
    for priority, pattern, prefix in patterns:
        for match in pattern.finditer(text):
            if prefix in {"ISO", "IEC"} and any(start <= match.start() < end for start, end in occupied_iso_iec):
                continue
            if prefix == "ISO/IEC":
                occupied_iso_iec.append(match.span())
            if prefix == "ASTM":
                number = f"{match.group(1)}{match.group(2)}"
            else:
                number = match.group(1)
            number = _strip_year_part(number)
            found.append((match.start(), priority, f"{prefix} {number}"))
    found.sort(key=lambda item: (item[0], item[1]))
    return _dedupe([family for _, _, family in found])


def _family_occurrence_count(text: str, family: str) -> int:
    return len(_family_years_in_text(text, family)) + normalize_identifier(text).count(normalize_identifier(family))


def _family_years_in_text(value: object, family: str) -> list[int]:
    text = normalize_identifier(value)
    family_pattern = re.escape(normalize_identifier(family)).replace(r"\ ", r"\s*")
    pattern = re.compile(
        family_pattern + r"\s*(?:[:\-]\s*|\s+)(?:ED\.?(?:ITION)?\s*)?((?:19|20)\d{2})(?!\d)",
        re.IGNORECASE,
    )
    return [int(match.group(1)) for match in pattern.finditer(text)]


def _years_in_text(value: object) -> list[int]:
    return [int(match.group(1)) for match in _YEAR_RE.finditer(normalize_identifier(value))]


def _strip_year_part(number: str) -> str:
    parts = number.split("-")
    if len(parts) > 1 and re.fullmatch(r"(?:19|20)\d{2}", parts[-1]):
        return "-".join(parts[:-1])
    return number


def _load_alias_rows(workbook_path: Path, sheet_name: str) -> list[_WorkbookAliasRow]:
    path = Path(workbook_path)
    if not path.is_file():
        raise RegulationIdentityError("workbook_not_found", f"Workbook does not exist: {path}")
    try:
        frame = pd.read_excel(path, sheet_name=sheet_name, dtype=object)
    except ValueError as exc:
        raise RegulationIdentityError(
            "sheet_not_found",
            f"Workbook sheet '{sheet_name}' does not exist.",
        ) from exc
    except Exception as exc:
        raise RegulationIdentityError("workbook_unreadable", f"Workbook could not be read: {exc}") from exc

    columns = {normalize_identifier(column): column for column in frame.columns}
    required = {
        "序号": "row_number",
        "原标准编号": "original_identifier",
        "原版本年份": "original_year",
        "最新版本": "latest_identifier",
        "最新年份": "latest_year",
    }
    missing = [name for name in required if normalize_identifier(name) not in columns]
    if missing:
        raise RegulationIdentityError(
            "workbook_columns_missing",
            f"Workbook sheet '{sheet_name}' is missing required columns: {', '.join(missing)}.",
        )

    source_columns = {name: columns[normalize_identifier(name)] for name in required}
    rows: list[_WorkbookAliasRow] = []
    for _, item in frame.iterrows():
        row_number = _numbered_row(item[source_columns["序号"]])
        if row_number is None:
            continue
        original_identifier = _cell_text(item[source_columns["原标准编号"]])
        latest_identifier = _cell_text(item[source_columns["最新版本"]])
        if not original_identifier and not latest_identifier:
            continue
        original_family = extract_regulation_family(original_identifier)
        latest_family = extract_regulation_family(latest_identifier)
        if not original_family and not latest_family:
            continue
        original_year = _cell_year(item[source_columns["原版本年份"]], original_identifier)
        latest_year = _cell_year(item[source_columns["最新年份"]], latest_identifier)
        rows.append(
            _WorkbookAliasRow(
                row_number=row_number,
                original_identifier=normalize_identifier(original_identifier),
                original_year=original_year,
                original_family=original_family,
                latest_identifier=normalize_identifier(latest_identifier),
                latest_year=latest_year,
                latest_family=latest_family,
            )
        )
    return rows


def _numbered_row(value: object) -> int | None:
    if value is None or isinstance(value, bool) or pd.isna(value):
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if not re.fullmatch(r"\d+(?:\.0+)?", text):
        return None
    number = int(float(text))
    return number if number > 0 else None


def _cell_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return unicodedata.normalize("NFKC", str(value)).strip()


def _cell_year(value: object, identifier: str) -> int | None:
    text = _cell_text(value)
    years = _years_in_text(text)
    if len(set(years)) == 1:
        return years[0]
    identifier_years = _years_in_text(identifier)
    return identifier_years[-1] if identifier_years else None


def _latest_edition_group(identifier: str, family: str) -> str:
    normalized = normalize_identifier(identifier)
    family_name = normalize_identifier(family)
    start = normalized.find(family_name)
    tail = normalized[start + len(family_name) :] if start >= 0 else normalized
    tail = _YEAR_RE.sub("", tail)
    tail = re.sub(r"^[\s:/+\-]+|[\s:/+\-]+$", "", tail)
    tail = re.sub(r"[\s:/+\-]+", " ", tail).strip()
    return f"{family_name}|{tail or 'BASE'}"


def _aggregate_family_aliases(rows: list[_WorkbookAliasRow], family: str) -> tuple[str, ...]:
    aliases = {family}
    for row in rows:
        if row.original_family != family and row.latest_family != family:
            continue
        for identifier in (row.original_identifier, row.latest_identifier):
            if not identifier:
                continue
            aliases.add(identifier)
            aliases.update(_families_in_text(identifier))
            without_years = _YEAR_RE.sub("", identifier)
            without_years = re.sub(r"[\s:/+\-]+$", "", without_years).strip()
            if without_years:
                aliases.add(without_years)
    return tuple(sorted(aliases, key=lambda item: (item != family, item)))


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


__all__ = [
    "ParsedRegulationIdentity",
    "ParsedUploadIdentity",
    "RegulationIdentityError",
    "ResolvedRegulationPair",
    "extract_regulation_family",
    "normalize_identifier",
    "parse_upload_identity",
    "resolve_regulation_pair",
]
