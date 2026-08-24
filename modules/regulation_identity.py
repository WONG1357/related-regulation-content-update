from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
            "warnings": list(self.warnings),
        }


def resolve_regulation_pair(
    first_file: Path,
    second_file: Path,
    *,
    first_name: str | None = None,
    second_name: str | None = None,
) -> ResolvedRegulationPair:
    """Resolve and order two editions whose filenames share one regulation name."""

    first = parse_upload_identity(first_file, file_name=first_name)
    second = parse_upload_identity(second_file, file_name=second_name)
    if first.family != second.family:
        raise RegulationIdentityError(
            "filename_mismatch",
            "The uploaded filenames do not share the same regulation name after removing "
            f"the edition year: {first.family} and {second.family}.",
            {"first": first.to_dict(), "second": second.to_dict()},
        )
    if first.year == second.year:
        raise RegulationIdentityError(
            "same_edition",
            f"Both uploaded files resolve to {first.family} {first.year}; two different editions are required.",
            {"first": first.to_dict(), "second": second.to_dict()},
        )

    old, latest = sorted((first, second), key=lambda item: item.year)
    return ResolvedRegulationPair(
        old=old,
        latest=latest,
        family=latest.family,
        old_year=old.year,
        latest_year=latest.year,
        match_mode="filename_match",
        aliases=_filename_aliases(latest.family),
    )


def parse_upload_identity(file_path: Path, *, file_name: str | None = None) -> ParsedRegulationIdentity:
    path = Path(file_path)
    if not path.is_file():
        raise RegulationIdentityError("file_not_found", f"Regulation file does not exist: {path}")

    display_name = Path(file_name or path.name).name
    normalized_name = normalize_identifier(display_name)
    filename_years = _years_in_text(normalized_name)
    if not filename_years:
        raise RegulationIdentityError(
            "year_missing",
            f"No four-digit edition year could be identified in filename: {display_name}.",
        )
    if len(set(filename_years)) > 1:
        raise RegulationIdentityError(
            "year_ambiguous",
            f"Multiple edition years were found in filename: {display_name}.",
            {"candidates": sorted(set(filename_years))},
        )

    year = filename_years[0]
    family = _filename_family(normalized_name)
    if not family:
        raise RegulationIdentityError(
            "family_missing",
            f"No regulation name remains after removing the edition year from filename: {display_name}.",
        )

    return ParsedRegulationIdentity(
        file_path=str(path.resolve()),
        file_name=display_name,
        normalized_file_name=normalized_name,
        family=family,
        year=year,
        family_source="filename",
        year_source="filename",
        detected_families=(family,),
        detected_years=tuple(sorted(set(filename_years))),
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


def _filename_family(normalized_name: str) -> str:
    family = _YEAR_RE.sub("", normalized_name)
    family = re.sub(r"[\s:_/+\-]+(?=[(\[{])", "", family)
    family = re.sub(r"[(\[{]\s*[)\]}]", "", family)
    family = family.strip(" _-:/+,")
    return re.sub(r"\s+", " ", family)


def _filename_aliases(family: str) -> tuple[str, ...]:
    aliases = [family]
    without_trailing_tag = re.sub(r"\s*\([A-Z]{1,8}\)$", "", family).strip()
    if without_trailing_tag and without_trailing_tag != family:
        aliases.append(without_trailing_tag)
    return tuple(aliases)


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


def _years_in_text(value: object) -> list[int]:
    return [int(match.group(1)) for match in _YEAR_RE.finditer(normalize_identifier(value))]


def _strip_year_part(number: str) -> str:
    parts = number.split("-")
    if len(parts) > 1 and re.fullmatch(r"(?:19|20)\d{2}", parts[-1]):
        return "-".join(parts[:-1])
    return number


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
