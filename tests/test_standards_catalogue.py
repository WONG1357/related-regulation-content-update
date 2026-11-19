"""Synthetic, schema-grounded cases: these are not real ISO publications."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.standards_catalogue import (
    ISO_SOURCE_URL,
    CatalogueFetchError,
    CatalogueValidationError,
    discover_iso_updates,
    fetch_iso_catalogue,
    load_iso_catalogue,
)


def publication(identifier: int, reference: str = "ISO 99999:2026", **changes: object) -> dict:
    """Create an invented record matching the observed official nullable schema."""
    raw = {
        "id": identifier, "reference": reference, "deliverableType": "IS",
        "supplementType": None, "title": {"en": "Synthetic medical device standard"},
        "publicationDate": "2026-06-01", "edition": 2, "icsCode": ["11.040.01"],
        "ownerCommittee": "ISO/TC 999", "currentStage": 6060,
        "replaces": None, "replacedBy": None, "scope": {"en": "Synthetic risk management scope"},
    }
    raw.update(changes)
    return raw


class FakeResponse(io.BytesIO):
    def __init__(self, payload: bytes, *, length: str | None = None, url: str = ISO_SOURCE_URL,
                 last_modified: str | None = None):
        super().__init__(payload)
        self.headers = {} if length is None else {"Content-Length": length}
        if last_modified is not None:
            self.headers["Last-Modified"] = last_modified
        self.url = url

    def geturl(self) -> str:
        return self.url


class StandardsCatalogueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def load(self, *rows: dict):
        path = self.root / "catalogue.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return load_iso_catalogue(path, retrieved_at="2026-11-19T10:00:00+08:00")

    def discover(self, *rows: dict, **filters: object) -> dict:
        return discover_iso_updates(self.load(*rows), date(2026, 1, 1), date(2026, 11, 19), **filters)

    def test_date_boundaries_are_inclusive_and_year_does_not_select_event(self) -> None:
        snapshot = self.load(
            publication(1, publicationDate="2025-11-18"),
            publication(2, "ISO 99999:2025", publicationDate="2025-11-19"),
            publication(3, publicationDate="2026-11-19"),
            publication(4, publicationDate="2026-11-20"),
        )
        result = discover_iso_updates(snapshot, date(2025, 11, 19), date(2026, 11, 19))
        self.assertEqual([row["id"] for row in result["updates"]], [3, 2])
        self.assertEqual(result["query"]["event_date_field"], "publicationDate")

    def test_leap_day_is_supported_and_impossible_date_rejected(self) -> None:
        snapshot = self.load(publication(1, publicationDate="2024-02-29"))
        result = discover_iso_updates(snapshot, date(2024, 2, 29), date(2024, 2, 29))
        self.assertEqual(len(result["updates"]), 1)
        with self.assertRaisesRegex(CatalogueValidationError, "publicationDate"):
            self.load(publication(1, publicationDate="2025-02-29"))

    def test_current_review_confirmation_and_withdrawal_do_not_hide_publication(self) -> None:
        result = self.discover(*(publication(i, currentStage=stage)
                                 for i, stage in enumerate([6060, 9020, 9092, 9093, 9599], 1)))
        self.assertEqual(len(result["updates"]), 5)
        self.assertEqual({row["current_stage"] for row in result["updates"]}, {6060, 9020, 9092, 9093, 9599})

    def test_draft_and_missing_date_are_excluded_without_inventing_dates(self) -> None:
        result = self.discover(
            publication(1, "ISO 99999:2025/AWI Amd 1", supplementType="Amd", currentStage=2000, publicationDate=None),
            publication(2, "ISO 99998:2026", currentStage=6060, publicationDate=None),
        )
        self.assertEqual(result["updates"], [])
        self.assertEqual(result["coverage"]["records_without_publication_date_in_scope"], 2)
        self.assertEqual(result["coverage"]["status"], "zero_matches_in_snapshot")

    def test_draft_with_claimed_publication_date_fails_validation(self) -> None:
        with self.assertRaisesRegex(CatalogueValidationError, "unpublished"):
            self.load(publication(1, currentStage=4020))

    def test_supplements_use_their_own_release_date_and_retain_distinct_ids(self) -> None:
        result = self.discover(
            publication(1, "ISO 99999:2016", publicationDate="2016-01-01", currentStage=9093),
            publication(2, "ISO 99999:2016/Amd 1:2026", supplementType="Amd"),
            publication(3, "ISO 99999:2016/Cor 1:2026", supplementType="Cor"),
            standards=["ISO 99999:2016"],
        )
        self.assertEqual({row["event_type"] for row in result["updates"]}, {"amendment", "corrigendum"})
        self.assertEqual({row["id"] for row in result["updates"]}, {2, 3})
        self.assertTrue(all(row["relationship_warnings"] for row in result["updates"]))

    def test_no_predecessor_is_first_edition_only_when_edition_one(self) -> None:
        result = self.discover(publication(1, edition=1), publication(2, edition=3), publication(3, edition=None))
        types = {row["id"]: row["event_type"] for row in result["updates"]}
        self.assertEqual(types, {1: "first_edition", 2: "publication_predecessor_unknown", 3: "publication_predecessor_unknown"})

    def test_multi_predecessors_and_missing_ids_remain_visible(self) -> None:
        result = self.discover(
            publication(1, "ISO 88888:2015", publicationDate="2015-01-01", currentStage=9599),
            publication(2, "ISO 99999:2018", publicationDate="2018-01-01", currentStage=9599),
            publication(3, replaces=[1, 2, 404], replacedBy=[999]),
        )
        row = result["updates"][0]
        self.assertEqual(row["replaces"], [1, 2, 404])
        self.assertEqual(row["replaced_by"], [999])
        self.assertEqual([p["id"] for p in row["predecessors"]], [1, 2, 404])
        self.assertFalse(row["predecessors"][-1]["available"])
        self.assertEqual(row["event_type"], "replacement_publication")
        self.assertEqual(row["comparison_status"], "metadata_only")
        self.assertGreaterEqual(len(row["relationship_warnings"]), 2)

    def test_cross_family_replacement_is_not_named_a_same_family_new_edition(self) -> None:
        result = self.discover(
            publication(1, "ISO 88888:2015", publicationDate="2015-01-01"),
            publication(2, "ISO 99999:2026", replaces=[1]),
            publication(3, "ISO 88888:2026", replaces=[1]),
        )
        updates = {row["id"]: row for row in result["updates"]}
        self.assertEqual(updates[2]["event_type"], "replacement_publication")
        self.assertTrue(any("cross-family" in warning for warning in updates[2]["relationship_warnings"]))
        self.assertEqual(updates[3]["event_type"], "new_edition")

    def test_future_and_self_predecessor_chronology_is_not_verified(self) -> None:
        result = self.discover(
            publication(1, publicationDate="2027-01-01"),
            publication(2, replaces=[1, 2]),
        )
        self.assertEqual(len(result["updates"]), 1)
        self.assertTrue(all(not p["chronology_verified"] for p in result["updates"][0]["predecessors"]))

    def test_issuer_type_adoption_and_part_are_not_collapsed(self) -> None:
        rows = [publication(i, reference) for i, reference in enumerate([
            "ISO 99999:2026", "ISO/IEC 99999:2026", "ISO/TR 99999:2026", "EN ISO 99999:2026",
            "ISO 99999-1:2026", "ISO 999990:2026",
        ], 1)]
        result = self.discover(*rows, standards=["ISO 99999"])
        self.assertEqual([row["id"] for row in result["updates"]], [1])
        result = self.discover(*rows, standards=["iso/iec 99999:2022", "ISO/TR 99999"])
        self.assertEqual({row["id"] for row in result["updates"]}, {2, 3})

    def test_series_expansion_requires_explicit_wildcard(self) -> None:
        rows = [publication(i, reference) for i, reference in enumerate([
            "ISO 99999:2026", "ISO 99999-1:2026", "ISO 99999-2-1:2026", "ISO/IEC 99999-1:2026", "ISO 999990-1:2026",
        ], 1)]
        result = self.discover(*rows, standards=["ISO 99999-*"])
        self.assertEqual({row["id"] for row in result["updates"]}, {2, 3})
        with self.assertRaises(CatalogueValidationError):
            self.discover(*rows, standards=["*99999*"])

    def test_filter_values_are_ored_and_fields_anded(self) -> None:
        result = self.discover(
            publication(1, "ISO 99999:2026", icsCode=["11.040.01"]),
            publication(2, "ISO 99998:2026", icsCode=["11.100.20"]),
            publication(3, "ISO 99997:2026", icsCode=["11.040.01"]),
            publication(4, "ISO 99999:2026", icsCode=["03.100.70"]),
            publication(5, "ISO 99999:2026", ownerCommittee="ISO/TC 998"),
            standards=["ISO 99999", "ISO 99998"], ics_prefixes=["11.040", "11.100"],
            committees=["iso/tc 999"], keywords=["no-match", "risk management"],
        )
        self.assertEqual({row["id"] for row in result["updates"]}, {1, 2})

    def test_nullable_arrays_edition_and_text_are_supported(self) -> None:
        result = self.discover(publication(1, edition=None, icsCode=None, title={"en": None, "fr": "Titre"}, scope={"en": None}))
        self.assertEqual(result["updates"][0]["title"], "Titre")
        self.assertEqual(result["updates"][0]["ics_codes"], [])

    def test_invalid_queries_fail_instead_of_broadening_scope(self) -> None:
        snapshot = self.load(publication(1))
        cases = [dict(standards="ISO 99999"), dict(standards=["99999"]), dict(standards=[""]),
                 dict(ics_prefixes=["1"]), dict(ics_prefixes=["11.04"]), dict(committees=[None]), dict(keywords=None)]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(CatalogueValidationError):
                discover_iso_updates(snapshot, date(2026, 1, 1), date(2026, 11, 19), **kwargs)
        for start, end in [("2026-01-01", date(2026, 11, 19)), (date(2026, 11, 20), date(2026, 1, 1)),
                           (datetime(2026, 1, 1), date(2026, 11, 19))]:
            with self.subTest(start=start), self.assertRaises(CatalogueValidationError):
                discover_iso_updates(snapshot, start, end)

    def test_missing_fields_and_wrong_types_fail_closed(self) -> None:
        invalid = [publication(True), publication(1, edition=0), publication(1, replaces="2"),
                   publication(1, replacedBy=[2, 2]), publication(1, title="title"),
                   publication(1, currentStage="6060"), publication(1, supplementType="unknown"),
                   publication(1, publicationDate="2026-06")]
        missing = publication(1)
        del missing["publicationDate"]
        invalid.append(missing)
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(CatalogueValidationError):
                self.load(row)

    def test_duplicate_ids_duplicate_json_keys_and_non_json_fail(self) -> None:
        with self.assertRaisesRegex(CatalogueValidationError, "Duplicate catalogue ID"):
            self.load(publication(1), publication(1))
        path = self.root / "bad.jsonl"
        for content in ['{"id":1,"id":2}\n', '<html>Unavailable</html>', '{"id":NaN}', '\ufffd', '[]']:
            path.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(CatalogueValidationError):
                load_iso_catalogue(path)

    def test_empty_or_unreadable_source_is_not_a_successful_zero(self) -> None:
        with self.assertRaisesRegex(CatalogueValidationError, "no records"):
            self.load()
        with self.assertRaisesRegex(CatalogueValidationError, "Cannot read"):
            load_iso_catalogue(self.root / "missing.jsonl")

    def test_provenance_coverage_and_json_serializability(self) -> None:
        result = self.discover(publication(1))
        self.assertEqual(result["source"]["sha256"], hashlib.sha256((self.root / "catalogue.jsonl").read_bytes()).hexdigest())
        self.assertEqual(result["source"]["license"], "ODC-By-1.0")
        self.assertIn("ISO Open Data", result["source"]["attribution"])
        self.assertFalse(result["coverage"]["full_text_compared"])
        self.assertFalse(result["coverage"]["lifecycle_event_history_available"])
        self.assertEqual(result["coverage"]["historical_completeness"], "not_guaranteed")
        json.dumps(result, allow_nan=False)

    def test_requested_end_after_snapshot_is_disclosed(self) -> None:
        snapshot = self.load(publication(1))
        result = discover_iso_updates(snapshot, date(2026, 1, 1), date(2027, 1, 1))
        self.assertTrue(any("after this snapshot" in warning for warning in result["warnings"]))

    def test_retrieval_timestamp_requires_timezone(self) -> None:
        self.load(publication(1))
        for timestamp in ["2026-11-19", "yesterday"]:
            with self.assertRaises(CatalogueValidationError):
                load_iso_catalogue(self.root / "catalogue.jsonl", retrieved_at=timestamp)

    def test_fetch_success_uses_fixed_url_and_atomically_replaces_cache(self) -> None:
        payload = (json.dumps(publication(1)) + "\n").encode()
        cache = self.root / "iso_deliverables_metadata.jsonl"
        cache.write_bytes(b"previous cache")
        with patch("modules.standards_catalogue.build_opener") as factory:
            factory.return_value.open.return_value = FakeResponse(payload, length=str(len(payload)),
                                                                  last_modified="Thu, 19 Nov 2026 02:00:00 GMT")
            snapshot = fetch_iso_catalogue(self.root)
            request = factory.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, ISO_SOURCE_URL)
        self.assertEqual(cache.read_bytes(), payload)
        self.assertEqual(snapshot.acquisition, "official_https_fetch")
        self.assertEqual(snapshot.sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(list(self.root.glob(".iso-download-*")), [])
        self.assertEqual(snapshot.dataset_updated_at, "2026-11-19T02:00:00+00:00")
        self.assertEqual(snapshot.http_last_modified, "Thu, 19 Nov 2026 02:00:00 GMT")
        retained = Path(snapshot.snapshot_path)
        provenance = json.loads(Path(snapshot.provenance_path).read_text())
        self.assertEqual(retained.read_bytes(), payload)
        self.assertEqual(retained.stat().st_ino, cache.stat().st_ino)
        self.assertEqual(provenance["sha256"], snapshot.sha256)
        self.assertEqual(provenance["dataset_updated_at"], snapshot.dataset_updated_at)
        self.assertIn("not a guarantee", provenance["timestamp_note"])
        result = discover_iso_updates(snapshot, date(2026, 1, 1), date(2026, 11, 19))
        self.assertEqual(result["source"]["dataset_updated_at"], snapshot.dataset_updated_at)
        self.assertFalse(result["source"]["source_timestamp_is_completeness_boundary"])

    def test_unchanged_fetch_reuses_bytes_and_preserves_first_provenance(self) -> None:
        payload = (json.dumps(publication(1)) + "\n").encode()
        with patch("modules.standards_catalogue.build_opener") as factory:
            factory.return_value.open.return_value = FakeResponse(payload, last_modified="Thu, 19 Nov 2026 02:00:00 GMT")
            first = fetch_iso_catalogue(self.root)
            original_provenance = Path(first.provenance_path).read_bytes()
            first_inode = Path(first.snapshot_path).stat().st_ino
            factory.return_value.open.return_value = FakeResponse(payload, last_modified="Fri, 20 Nov 2026 02:00:00 GMT")
            second = fetch_iso_catalogue(self.root)
        self.assertEqual(first.snapshot_path, second.snapshot_path)
        self.assertEqual(Path(second.snapshot_path).stat().st_ino, first_inode)
        self.assertEqual(Path(second.provenance_path).read_bytes(), original_provenance)
        self.assertEqual(len(list((self.root / "snapshots").glob("*.jsonl"))), 1)
        self.assertEqual(second.dataset_updated_at, "2026-11-20T02:00:00+00:00")

    def test_changed_fetch_keeps_prior_snapshot_and_advances_latest(self) -> None:
        first_payload = (json.dumps(publication(1)) + "\n").encode()
        second_payload = (json.dumps(publication(2)) + "\n").encode()
        with patch("modules.standards_catalogue.build_opener") as factory:
            factory.return_value.open.return_value = FakeResponse(first_payload)
            first = fetch_iso_catalogue(self.root)
            factory.return_value.open.return_value = FakeResponse(second_payload)
            second = fetch_iso_catalogue(self.root)
        self.assertNotEqual(first.snapshot_path, second.snapshot_path)
        self.assertEqual(Path(first.snapshot_path).read_bytes(), first_payload)
        self.assertEqual((self.root / "iso_deliverables_metadata.jsonl").read_bytes(), second_payload)
        self.assertEqual(len(list((self.root / "snapshots").glob("*.jsonl"))), 2)
        self.assertEqual(len(list((self.root / "snapshots").glob("*.provenance.json"))), 2)

    def test_invalid_last_modified_remains_unknown_and_is_disclosed(self) -> None:
        payload = (json.dumps(publication(1)) + "\n").encode()
        with patch("modules.standards_catalogue.build_opener") as factory:
            factory.return_value.open.return_value = FakeResponse(payload, last_modified="not a date")
            snapshot = fetch_iso_catalogue(self.root)
        self.assertIsNone(snapshot.dataset_updated_at)
        self.assertTrue(any("Last-Modified" in warning for warning in snapshot.warnings))

    def test_provenance_failure_does_not_advance_latest(self) -> None:
        payload = (json.dumps(publication(1)) + "\n").encode()
        cache = self.root / "iso_deliverables_metadata.jsonl"
        cache.write_bytes(b"old snapshot")
        archive = self.root / "snapshots"
        archive.mkdir()
        digest = hashlib.sha256(payload).hexdigest()
        (archive / f"{digest}.provenance.json").write_text('{"sha256":"tampered"}')
        with patch("modules.standards_catalogue.build_opener") as factory:
            factory.return_value.open.return_value = FakeResponse(payload)
            with self.assertRaisesRegex(CatalogueFetchError, "provenance is inconsistent"):
                fetch_iso_catalogue(self.root)
        self.assertEqual(cache.read_bytes(), b"old snapshot")
        self.assertEqual(list(self.root.glob(".iso-current-*")), [])
        self.assertEqual(list(archive.glob(".iso-provenance-*")), [])

    def test_latest_pointer_failure_preserves_previous_cache(self) -> None:
        payload = (json.dumps(publication(1)) + "\n").encode()
        cache = self.root / "iso_deliverables_metadata.jsonl"
        cache.write_bytes(b"old snapshot")
        with patch("modules.standards_catalogue.build_opener") as factory, patch("modules.standards_catalogue.os.replace", side_effect=OSError("failed publication")):
            factory.return_value.open.return_value = FakeResponse(payload)
            with self.assertRaises(CatalogueFetchError):
                fetch_iso_catalogue(self.root)
        self.assertEqual(cache.read_bytes(), b"old snapshot")
        self.assertEqual(list(self.root.glob(".iso-current-*")), [])
        self.assertEqual(len(list((self.root / "snapshots").glob("*.provenance.json"))), 1)

    def test_corrupt_existing_archive_is_not_overwritten(self) -> None:
        payload = (json.dumps(publication(1)) + "\n").encode()
        archive = self.root / "snapshots"
        archive.mkdir()
        retained = archive / f"{hashlib.sha256(payload).hexdigest()}.jsonl"
        retained.write_bytes(b"tampered")
        with patch("modules.standards_catalogue.build_opener") as factory:
            factory.return_value.open.return_value = FakeResponse(payload)
            with self.assertRaisesRegex(CatalogueFetchError, "inconsistent content"):
                fetch_iso_catalogue(self.root)
        self.assertEqual(retained.read_bytes(), b"tampered")
        self.assertFalse((self.root / "iso_deliverables_metadata.jsonl").exists())

    def test_network_failure_does_not_silently_use_or_overwrite_cache(self) -> None:
        cache = self.root / "iso_deliverables_metadata.jsonl"
        cache.write_bytes(b"old snapshot")
        with patch("modules.standards_catalogue.build_opener") as factory:
            factory.return_value.open.side_effect = URLError("offline")
            with self.assertRaisesRegex(CatalogueFetchError, "no cached results"):
                fetch_iso_catalogue(self.root)
        self.assertEqual(cache.read_bytes(), b"old snapshot")
        self.assertEqual(list(self.root.glob(".iso-download-*")), [])

    def test_corrupt_short_oversize_or_redirected_download_keeps_cache(self) -> None:
        cache = self.root / "iso_deliverables_metadata.jsonl"
        cache.write_bytes(b"old snapshot")
        responses = [FakeResponse(b"not JSON"), FakeResponse(b"{}", length="3"),
                     FakeResponse(b"{}", length=str(300 * 1024 * 1024)),
                     FakeResponse(b"{}", url="https://example.invalid/catalogue")]
        for response in responses:
            with self.subTest(response=response), patch("modules.standards_catalogue.build_opener") as factory:
                factory.return_value.open.return_value = response
                with self.assertRaises(CatalogueFetchError):
                    fetch_iso_catalogue(self.root)
                self.assertEqual(cache.read_bytes(), b"old snapshot")
                self.assertEqual(list(self.root.glob(".iso-download-*")), [])
        with patch("modules.standards_catalogue.MAX_DOWNLOAD_BYTES", 2), patch("modules.standards_catalogue.build_opener") as factory:
            factory.return_value.open.return_value = FakeResponse(b"123")
            with self.assertRaises(CatalogueFetchError):
                fetch_iso_catalogue(self.root)


if __name__ == "__main__":
    unittest.main()
