"""Deterministic ISO Open Data discovery; no full-text or LLM access.

The catalogue is a *current snapshot*, not a complete lifecycle event archive.
Publication dates select events; current stages never substitute for dates.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


ISO_SOURCE_URL = (
    "https://isopublicstorageprod.blob.core.windows.net/opendata/_latest/"
    "iso_deliverables_metadata/json/iso_deliverables_metadata.jsonl"
)
ISO_OPEN_DATA_URL = "https://www.iso.org/open-data.html"
ISO_ATTRIBUTION = (
    "This work is based on the iso_deliverables_metadata dataset from ISO Open "
    "Data, licensed under ODC Attribution License (ODC-By) v1.0."
)
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
MAX_LINE_BYTES = 4 * 1024 * 1024
_REQUIRED_FIELDS = {
    "id", "deliverableType", "supplementType", "reference", "title",
    "publicationDate", "edition", "icsCode", "ownerCommittee", "currentStage",
    "replaces", "replacedBy",
}
_SUPPLEMENT_EVENTS = {
    "amd": "amendment", "cor": "corrigendum", "add": "addendum",
    "ext": "extension", "suppl": "supplement",
}
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_FAMILY_RE = re.compile(
    r"[A-Z][A-Z0-9]*(?:/[A-Z][A-Z0-9]*)*(?: [A-Z]+)* "
    r"\d+[A-Z0-9]*(?:-[A-Z0-9]+)*(?:-\*)?\Z"
)


class CatalogueError(ValueError):
    """A source or query cannot be used safely for standards discovery."""


class CatalogueValidationError(CatalogueError):
    """Missing, malformed, ambiguous or unexpectedly shaped source data."""


class CatalogueFetchError(CatalogueError):
    """The official snapshot could not be fetched; no stale fallback is used."""


@dataclass(frozen=True)
class CatalogueRecord:
    id: int
    reference: str
    title: str
    family: str
    publication_date: date | None
    current_stage: int
    edition: int | None
    deliverable_type: str
    supplement_type: str | None
    ics_codes: tuple[str, ...]
    owner_committee: str
    replaces: tuple[int, ...]
    replaced_by: tuple[int, ...]
    scope: str = ""

    @property
    def source_url(self) -> str:
        return f"https://www.iso.org/standard/{self.id}.html"


@dataclass(frozen=True)
class CatalogueSnapshot:
    records: dict[int, CatalogueRecord]
    source_url: str
    retrieved_at: str
    sha256: str
    warnings: tuple[str, ...] = ()
    acquisition: str = "local_file"
    dataset_updated_at: str | None = None
    http_last_modified: str | None = None
    snapshot_path: str | None = None
    provenance_path: str | None = None


def _normalized_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = value.translate(str.maketrans({"–": "-", "—": "-", "−": "-", "‑": "-"}))
    return re.sub(r"\s+", " ", value).strip().upper()


def normalize_standard_family(reference: str) -> str:
    """Remove the edition and supplement suffix, retaining issuer/type/part.

    ISO 123, ISO/IEC 123, ISO/TR 123 and EN ISO 123 stay distinct. A bare
    ISO 10993 does not match its parts; use the explicit ISO 10993-* pattern.
    """
    if not isinstance(reference, str) or not reference.strip():
        raise CatalogueValidationError("A standard reference must be a non-empty string.")
    family = _normalized_text(reference).split(":", 1)[0]
    family = re.split(r"/(?:AMD|COR|ADD|EXT|SUPPL)\b", family, maxsplit=1)[0]
    return family.strip()


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CatalogueValidationError(f"Duplicate JSON field: {key}.")
        result[key] = value
    return result


def _invalid_json_constant(value: str) -> None:
    raise CatalogueValidationError(f"Non-JSON numeric constant: {value}.")


def _positive_int(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise CatalogueValidationError(f"{name} must be a positive integer.")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogueValidationError(f"{name} must be a non-empty string.")
    return value.strip()


def _ids(value: Any, name: str) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise CatalogueValidationError(f"{name} must be an array or null.")
    values = tuple(_positive_int(item, name) for item in value)
    if len(set(values)) != len(values):
        raise CatalogueValidationError(f"{name} contains duplicate IDs.")
    return values


def _language_text(value: Any, name: str) -> str:
    if not isinstance(value, dict):
        raise CatalogueValidationError(f"{name} must be a language-to-text object.")
    if any(not isinstance(k, str) or (v is not None and not isinstance(v, str))
           for k, v in value.items()):
        raise CatalogueValidationError(f"{name} contains invalid language/text values.")
    return value.get("en") or next((v for v in value.values() if v), "")


def _publication_date(value: Any) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise CatalogueValidationError("publicationDate must be YYYY-MM-DD or null.")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CatalogueValidationError(f"Invalid publicationDate: {value}.") from exc


def _record(raw: Any) -> CatalogueRecord:
    if not isinstance(raw, dict):
        raise CatalogueValidationError("Each JSONL row must be an object.")
    missing = _REQUIRED_FIELDS - raw.keys()
    if missing:
        raise CatalogueValidationError(f"Missing catalogue fields: {', '.join(sorted(missing))}.")
    reference = _string(raw["reference"], "reference")
    supplement = raw["supplementType"]
    if supplement is not None:
        supplement = _string(supplement, "supplementType").lower()
        if supplement not in _SUPPLEMENT_EVENTS:
            raise CatalogueValidationError(f"Unknown supplementType: {supplement}.")
    edition = raw["edition"]
    if edition is not None:
        edition = _positive_int(edition, "edition")
    stage = raw["currentStage"]
    if type(stage) is not int or not 0 <= stage <= 9999:
        raise CatalogueValidationError("currentStage must be an integer from 0 to 9999.")
    codes = raw["icsCode"]
    if codes is None:
        codes = []
    if not isinstance(codes, list) or any(not isinstance(c, str) or not c.strip() for c in codes):
        raise CatalogueValidationError("icsCode must be an array of non-empty strings or null.")
    title = _language_text(raw["title"], "title")
    scope = _language_text(raw.get("scope", {}), "scope")
    publication_date = _publication_date(raw["publicationDate"])
    if publication_date is not None and stage < 6060:
        raise CatalogueValidationError("publicationDate conflicts with an unpublished currentStage.")
    return CatalogueRecord(
        id=_positive_int(raw["id"], "id"), reference=reference, title=title or reference,
        family=normalize_standard_family(reference),
        publication_date=publication_date, current_stage=stage,
        edition=edition, deliverable_type=_string(raw["deliverableType"], "deliverableType").upper(),
        supplement_type=supplement, ics_codes=tuple(codes),
        owner_committee=_string(raw["ownerCommittee"], "ownerCommittee"),
        replaces=_ids(raw["replaces"], "replaces"), replaced_by=_ids(raw["replacedBy"], "replacedBy"),
        scope=scope,
    )


def _timestamp(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise CatalogueValidationError("retrieved_at must be an ISO timestamp with a timezone.") from exc
    if parsed.tzinfo is None:
        raise CatalogueValidationError("retrieved_at must include a timezone.")
    return parsed.isoformat()


def load_iso_catalogue(
    path: str | Path, *, source_url: str = ISO_SOURCE_URL, retrieved_at: str | None = None
) -> CatalogueSnapshot:
    """Validate an entire local JSONL snapshot, failing on corruption/duplicates.

    ``retrieved_at`` should be passed from retained fetch provenance for imports.
    If omitted it is this load's timestamp, not a publisher update timestamp.
    A local file's contents are not authenticated merely by naming an ISO URL.
    """
    source_url = _string(source_url, "source_url")
    timestamp = _timestamp(retrieved_at)
    records: dict[int, CatalogueRecord] = {}
    checksum = hashlib.sha256()
    total = 0
    try:
        with Path(path).open("rb") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                total += len(raw_line)
                if total > MAX_DOWNLOAD_BYTES or len(raw_line) > MAX_LINE_BYTES:
                    raise CatalogueValidationError("Catalogue exceeds the configured size limit.")
                checksum.update(raw_line)
                if not raw_line.strip():
                    continue
                try:
                    line = raw_line.decode("utf-8-sig" if line_number == 1 else "utf-8")
                    raw = json.loads(line, object_pairs_hook=_strict_object, parse_constant=_invalid_json_constant)
                    row = _record(raw)
                    if row.id in records:
                        raise CatalogueValidationError(f"Duplicate catalogue ID {row.id}.")
                    records[row.id] = row
                except (UnicodeError, json.JSONDecodeError, CatalogueValidationError) as exc:
                    raise CatalogueValidationError(f"Invalid catalogue line {line_number}: {exc}") from exc
    except OSError as exc:
        raise CatalogueValidationError(f"Cannot read catalogue: {exc}") from exc
    if not records:
        raise CatalogueValidationError("Catalogue contains no records; this is not a successful zero-result search.")
    warnings = (
        "This is a current catalogue snapshot. Completeness of historical publications is not guaranteed.",
        "Confirmation, withdrawal and draft-transition event dates are not available in this dataset schema.",
        "Publication metadata and predecessor links cannot establish clause-level changes or regulatory applicability.",
    )
    return CatalogueSnapshot(records, source_url, timestamp, checksum.hexdigest(), warnings)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        raise CatalogueFetchError("The official catalogue redirected; redirect was not followed.")


def _last_modified(value: str | None) -> str | None:
    """Return HTTP object modification time, never an event-coverage cutoff."""
    if value is None:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _file_hash(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def _persist_snapshot(temporary: Path, directory: Path, snapshot: CatalogueSnapshot) -> CatalogueSnapshot:
    """Keep immutable content/provenance, then atomically advance the latest link.

    Every persistent JSONL name is a hard link to retained content, so fetching
    unchanged bytes does not create another stored copy. A failed transaction
    can leave an unreferenced immutable archive, but cannot advance the latest
    pointer before both its content and provenance are validated.
    """
    archive = directory / "snapshots"
    archive.mkdir(exist_ok=True)
    content_path = archive / f"{snapshot.sha256}.jsonl"
    provenance_path = archive / f"{snapshot.sha256}.provenance.json"
    try:
        os.link(temporary, content_path)
        os.chmod(content_path, 0o444)
    except FileExistsError:
        if content_path.is_symlink() or _file_hash(content_path) != snapshot.sha256:
            raise CatalogueFetchError("An existing hash-named snapshot has inconsistent content; it was not overwritten.")

    metadata = {
        "schema_version": 1,
        "dataset": "iso_deliverables_metadata",
        "source_url": snapshot.source_url,
        "retrieved_at": snapshot.retrieved_at,
        "dataset_updated_at": snapshot.dataset_updated_at,
        "http_last_modified": snapshot.http_last_modified,
        "sha256": snapshot.sha256,
        "size_bytes": temporary.stat().st_size,
        "snapshot_filename": content_path.name,
        "acquisition": snapshot.acquisition,
        "attribution": ISO_ATTRIBUTION,
        "license": "ODC-By-1.0",
        "timestamp_note": "HTTP Last-Modified is source-object modification time, not a guarantee of event coverage or completeness.",
        "history_note": "Retained snapshots begin when fetched by this application; they do not backfill historical lifecycle events.",
    }
    metadata_temporary: Path | None = None
    latest_temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".iso-provenance-", suffix=".json", dir=archive, delete=False) as handle:
            metadata_temporary = Path(handle.name)
            json.dump(metadata, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(metadata_temporary, provenance_path)
            os.chmod(provenance_path, 0o444)
        except FileExistsError:
            if provenance_path.is_symlink():
                raise CatalogueFetchError("Snapshot provenance unexpectedly points to a symlink.")
            try:
                prior = json.loads(provenance_path.read_text(encoding="utf-8"), object_pairs_hook=_strict_object,
                                   parse_constant=_invalid_json_constant)
            except (UnicodeError, json.JSONDecodeError, CatalogueValidationError) as exc:
                raise CatalogueFetchError("Existing snapshot provenance is unreadable; it was not overwritten.") from exc
            if not isinstance(prior, dict) or any(prior.get(key) != metadata[key] for key in
                                                  ("schema_version", "source_url", "sha256", "size_bytes", "snapshot_filename", "acquisition")):
                raise CatalogueFetchError("Existing snapshot provenance is inconsistent; it was not overwritten.")
            if not isinstance(prior.get("retrieved_at"), str):
                raise CatalogueFetchError("Existing snapshot provenance lacks a valid retrieval timestamp.")
            _timestamp(prior["retrieved_at"])
            if prior.get("dataset_updated_at") != _last_modified(prior.get("http_last_modified")):
                raise CatalogueFetchError("Existing snapshot provenance has inconsistent source timestamps.")
            # An immutable sidecar records the first acquisition of identical
            # bytes. The returned snapshot records this latest successful fetch.

        fd, name = tempfile.mkstemp(prefix=".iso-current-", suffix=".jsonl", dir=directory)
        os.close(fd)
        latest_temporary = Path(name)
        latest_temporary.unlink()
        os.link(content_path, latest_temporary)
        os.replace(latest_temporary, directory / "iso_deliverables_metadata.jsonl")
        return replace(snapshot, snapshot_path=str(content_path.resolve()), provenance_path=str(provenance_path.resolve()))
    finally:
        for candidate in (metadata_temporary, latest_temporary):
            if candidate is not None:
                candidate.unlink(missing_ok=True)


def fetch_iso_catalogue(cache_dir: str | Path, *, timeout: float = 30) -> CatalogueSnapshot:
    """Fetch the fixed official URL and replace the cached file only after validation.

    Download/network/schema failures always raise; a previous cache is left
    untouched and is never returned as though a fresh request succeeded.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
        raise CatalogueValidationError("timeout must be a positive number no greater than 300 seconds.")
    directory = Path(cache_dir)
    temporary: Path | None = None
    http_last_modified: str | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".iso-download-", suffix=".jsonl", dir=directory)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as output:
            request = Request(ISO_SOURCE_URL, headers={"User-Agent": "StandardsUpdateAgent/1.0", "Accept": "application/octet-stream, application/json"})
            with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
                if response.geturl() != ISO_SOURCE_URL:
                    raise CatalogueFetchError("Catalogue response came from an unexpected URL.")
                http_last_modified = response.headers.get("Last-Modified")
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdigit() or int(length) > MAX_DOWNLOAD_BYTES):
                    raise CatalogueFetchError("Catalogue Content-Length is invalid or exceeds the size limit.")
                total = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise CatalogueFetchError("Catalogue download exceeds the size limit.")
                    output.write(chunk)
                if length is not None and total != int(length):
                    raise CatalogueFetchError("Catalogue download is incomplete (Content-Length mismatch).")
            output.flush()
            os.fsync(output.fileno())
        snapshot = load_iso_catalogue(temporary)
        updated_at = _last_modified(http_last_modified)
        warnings = snapshot.warnings
        if http_last_modified is not None and updated_at is None:
            warnings += ("The source's HTTP Last-Modified header could not be parsed; source update time remains unknown.",)
        snapshot = replace(snapshot, acquisition="official_https_fetch", dataset_updated_at=updated_at,
                           http_last_modified=http_last_modified, warnings=warnings)
        return _persist_snapshot(temporary, directory, snapshot)
    except (OSError, URLError, HTTPError, CatalogueError) as exc:
        raise CatalogueFetchError(f"Official ISO catalogue fetch failed; no cached results were substituted: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _query_strings(values: Iterable[str], name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise CatalogueValidationError(f"{name} must be a sequence of strings, not a single string.")
    try:
        return tuple(dict.fromkeys(_string(value, name) for value in values))
    except TypeError as exc:
        raise CatalogueValidationError(f"{name} must be a sequence of strings.") from exc


def _matches_family(family: str, pattern: str) -> bool:
    if pattern.endswith("-*"):
        return family.startswith(pattern[:-1]) and len(family) > len(pattern) - 1
    return family == pattern


def _event_type(record: CatalogueRecord, snapshot: CatalogueSnapshot) -> str:
    if record.supplement_type:
        return _SUPPLEMENT_EVENTS[record.supplement_type]
    if record.replaces:
        if any(identifier in snapshot.records and snapshot.records[identifier].family != record.family
               for identifier in record.replaces):
            return "replacement_publication"
        return "new_edition"
    if record.edition == 1:
        return "first_edition"
    return "publication_predecessor_unknown"


def _update(record: CatalogueRecord, snapshot: CatalogueSnapshot) -> dict[str, Any]:
    predecessors: list[dict[str, Any]] = []
    relationship_warnings: list[str] = []
    for predecessor_id in record.replaces:
        predecessor = snapshot.records.get(predecessor_id)
        if predecessor is None:
            predecessors.append({"id": predecessor_id, "reference": None, "publication_date": None,
                                 "available": False, "source_url": f"https://www.iso.org/standard/{predecessor_id}.html"})
            relationship_warnings.append(f"Predecessor {predecessor_id} is absent from this snapshot; its identity and text must be verified.")
            continue
        valid_earlier_date = (predecessor.publication_date is not None and record.publication_date is not None
                              and predecessor.publication_date <= record.publication_date and predecessor.id != record.id)
        if not valid_earlier_date:
            relationship_warnings.append(f"Predecessor {predecessor_id} has a missing/inconsistent publication date or self-reference; review before comparison.")
        predecessors.append({
            "id": predecessor.id, "reference": predecessor.reference, "family": predecessor.family,
            "title": predecessor.title, "publication_date": predecessor.publication_date.isoformat() if predecessor.publication_date else None,
            "edition": predecessor.edition, "supplement_type": predecessor.supplement_type,
            "current_stage": predecessor.current_stage, "source_url": predecessor.source_url,
            "available": True, "chronology_verified": valid_earlier_date,
        })
    if len(record.replaces) > 1:
        relationship_warnings.append("Multiple predecessor publications exist; preserve every relationship when selecting comparison documents.")
    if _event_type(record, snapshot) == "replacement_publication":
        relationship_warnings.append("A known predecessor has a different standard family; this is a cross-family replacement, not solely a same-family new edition.")
    if record.supplement_type:
        relationship_warnings.append("This supplement must be compared in the context of its correct base edition; replaces IDs do not necessarily identify that base.")
    elif not record.replaces and record.edition != 1:
        relationship_warnings.append("No predecessor is supplied; this does not establish a first edition or absence of earlier publications.")
    return {
        "id": record.id, "reference": record.reference, "title": record.title, "family": record.family,
        "publication_date": record.publication_date.isoformat() if record.publication_date else None,
        "event_type": _event_type(record, snapshot), "current_stage": record.current_stage,
        "edition": record.edition, "deliverable_type": record.deliverable_type,
        "supplement_type": record.supplement_type, "ics_codes": list(record.ics_codes),
        "owner_committee": record.owner_committee, "source_url": record.source_url,
        "replaces": list(record.replaces), "replaced_by": list(record.replaced_by),
        "predecessors": predecessors, "comparison_status": "metadata_only",
        "relationship_warnings": relationship_warnings,
    }


def discover_iso_updates(
    snapshot: CatalogueSnapshot, start_date: date, end_date: date, *,
    standards: Iterable[str] = (), ics_prefixes: Iterable[str] = (),
    committees: Iterable[str] = (), keywords: Iterable[str] = (),
) -> dict[str, Any]:
    """Find publications in an inclusive date window, with explicit scope logic.

    Values in one filter are ORed; different filters are ANDed. Keywords are
    literal case-insensitive substrings of reference/title/scope (not LLM terms).
    The result makes no claim that historical coverage or content comparison is complete.
    """
    if type(start_date) is not date or type(end_date) is not date or start_date > end_date:
        raise CatalogueValidationError("Use date objects with start_date on or before end_date.")
    if not isinstance(snapshot, CatalogueSnapshot) or not snapshot.records:
        raise CatalogueValidationError("A non-empty validated catalogue snapshot is required.")
    raw_standards = _query_strings(standards, "standards")
    families = tuple(dict.fromkeys(normalize_standard_family(value) for value in raw_standards))
    for family in families:
        if not _FAMILY_RE.fullmatch(family):
            raise CatalogueValidationError(f"Invalid standard scope '{family}'; include issuer/type/part, e.g. ISO 13485 or ISO 10993-*.")
    prefixes = _query_strings(ics_prefixes, "ics_prefixes")
    if any(not re.fullmatch(r"\d{2}(?:\.\d{3}(?:\.\d{2})?)?", value) for value in prefixes):
        raise CatalogueValidationError("ICS prefixes must be codes such as 11, 11.040 or 11.040.01.")
    committee_values = tuple(_normalized_text(v) for v in _query_strings(committees, "committees"))
    keyword_values = _query_strings(keywords, "keywords")
    folded_keywords = tuple(v.casefold() for v in keyword_values)
    scoped: list[CatalogueRecord] = []
    for row in snapshot.records.values():
        if families and not any(_matches_family(row.family, family) for family in families):
            continue
        if prefixes and not any(code == prefix or code.startswith(prefix + ".")
                                for prefix in prefixes for code in row.ics_codes):
            continue
        if committee_values and _normalized_text(row.owner_committee) not in committee_values:
            continue
        searchable = f"{row.reference}\n{row.title}\n{row.scope}".casefold()
        if folded_keywords and not any(word in searchable for word in folded_keywords):
            continue
        scoped.append(row)
    matches = [row for row in scoped if row.publication_date is not None
               and start_date <= row.publication_date <= end_date]
    matches.sort(key=lambda row: (row.publication_date, row.reference, row.id), reverse=True)
    published_dates = [row.publication_date for row in snapshot.records.values() if row.publication_date]
    retrieved = datetime.fromisoformat(snapshot.retrieved_at.replace("Z", "+00:00"))
    warnings = list(snapshot.warnings)
    if end_date > retrieved.date():
        warnings.append("The requested end date is after this snapshot was retrieved; later events cannot be covered.")
    if snapshot.acquisition == "local_file":
        warnings.append("Local import: the supplied source URL does not authenticate file origin, and retrieval time may be the local load time.")
    if not matches:
        warnings.append("No matching publications were found in this snapshot; this is not proof that no relevant updates occurred.")
    return {
        "updates": [_update(row, snapshot) for row in matches],
        "coverage": {
            "status": "matches_found" if matches else "zero_matches_in_snapshot",
            "catalogue_type": "current_snapshot", "historical_completeness": "not_guaranteed",
            "lifecycle_event_history_available": False, "publication_events_only": True,
            "total_records": len(snapshot.records), "records_in_scope": len(scoped),
            "records_without_publication_date_in_scope": sum(row.publication_date is None for row in scoped),
            "matched_publications": len(matches),
            "earliest_catalogue_publication": min(published_dates).isoformat() if published_dates else None,
            "latest_catalogue_publication": max(published_dates).isoformat() if published_dates else None,
            "coverage_bounds_are_not_completeness_guarantees": True,
            "full_text_compared": False, "regulatory_recognition_checked": False,
            "includes_currently_withdrawn_publications": True,
        },
        "warnings": warnings,
        "query": {
            "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
            "date_boundaries": "inclusive", "event_date_field": "publicationDate",
            "standards": list(raw_standards), "normalized_families": list(families),
            "ics_prefixes": list(prefixes), "committees": list(committee_values),
            "keywords": list(keyword_values), "scope_logic": "OR within a field; AND between fields",
        },
        "source": {
            "url": snapshot.source_url, "dataset": "iso_deliverables_metadata",
            "retrieved_at": snapshot.retrieved_at, "dataset_updated_at": snapshot.dataset_updated_at,
            "http_last_modified": snapshot.http_last_modified,
            "dataset_updated_at_basis": "HTTP Last-Modified" if snapshot.dataset_updated_at else None,
            "source_timestamp_is_completeness_boundary": False,
            "immutable_snapshot_retained": snapshot.snapshot_path is not None,
            "sha256": snapshot.sha256, "acquisition": snapshot.acquisition,
            "attribution": ISO_ATTRIBUTION, "attribution_url": ISO_OPEN_DATA_URL,
            "license": "ODC-By-1.0", "license_url": "https://opendatacommons.org/licenses/by/1-0/",
        },
    }
