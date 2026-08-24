from __future__ import annotations

import tempfile
import unittest
import sys
from pathlib import Path
import json

import pandas as pd
from docx import Document

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.amendment_applier import apply_amendment_to_source
from modules.document_cache import export_document_summaries, get_or_create_profile
from modules.document_extractor import (
    clean_chinese_only_line,
    extract_chinese_only_text,
    extract_document,
    extract_docx_sections,
    extract_main_sections,
    extract_section_references,
)
from modules.excel_loader import load_regulation_excel
from modules.impact_analyzer import analyze_impact, classify_priority, review_one_candidate, select_candidate_pairs
from modules.incremental_review_runner import (
    create_review_job,
    load_reviewer_decisions,
    load_review_job,
    load_review_results,
    request_stop_review_job,
    run_review_job,
    save_reviewer_decision,
)
from modules.reference_graph import attach_reference_matches, build_reference_map_rows, document_code_from_filename
from modules.regulation_pairing import build_file_regulation_pairing_map
from modules.reference_tree import build_reference_tree
from modules.regulation_normalizer import aliases_for
from modules.export_utils import dataframe_to_csv_bytes, dataframe_to_excel_bytes
from modules import agentic_amendment_workflow as amendment_workflow


class RegulatoryUpdateTests(unittest.TestCase):
    def test_excel_loader_selects_only_updated_status_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "updates.xlsx"
            pd.DataFrame(
                [
                    {"原标准编号": "ISO 14971:2019", "最新版本": "ISO 14971:2025", "最新年份": 2025, "状态": "已更新"},
                    {"原标准编号": "ISO 13485:2016", "最新版本": "", "最新年份": 2026, "状态": "需要确认"},
                    {"原标准编号": "IEC 62304:2006", "最新版本": "", "最新年份": 2026, "状态": "当前版本（再确认）"},
                ]
            ).to_excel(path, index=False, sheet_name="原始总表")

            result = load_regulation_excel(path)

        self.assertEqual(len(result.updated_regulations), 1)
        self.assertEqual(result.updated_regulations.iloc[0]["状态"], "已更新")
        self.assertEqual(result.stats["need_confirmation_rows"], 2)

    def test_broad_chinese_aliases_are_weak_only(self) -> None:
        aliases = aliases_for("ISO 14971:2019", "ISO 14971:2025")
        risk_aliases = [item for item in aliases if item["alias"] == "风险管理"]

        self.assertEqual(risk_aliases, [{"alias": "风险管理", "strength": "weak"}])
        priority, _ = classify_priority(pd.Series({"状态": "已更新"}), "风险管理", "weak")
        self.assertEqual(priority, "Low")

    def test_filename_code_normalization(self) -> None:
        cases = {
            "QSP0401-P DOCUMENT AND RECORD CONTROL文件记录控制(1).docx": "QSP0401",
            "QSP0704-N DESIGN AND DEVELOPMENT CONTROL AND NEW PRODUCT INTRODUCTION CONTROL - 20260618(1)(1).docx": "QSP0704",
            "WI-07-ENG-11-A  ENGINEERING CHANGE REQUEST CONTROL工程变更控制.doc": "WI-07-ENG-11",
            "QM01-N QUALITY MANUAL 质量手册.docx": "QM01",
        }
        for file_name, expected in cases.items():
            with self.subTest(file_name=file_name):
                self.assertEqual(document_code_from_filename(file_name), expected)

    def test_document_cache_reuses_same_filename_and_hash(self) -> None:
        document = sample_document("WI-01-A Test.docx", "P1", "Original text")
        calls = {"count": 0}

        def analyzer(_: dict) -> dict:
            calls["count"] += 1
            return {"content_summary": "summary", "key_sections": []}

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "cache.json"
            first = get_or_create_profile(document, analyzer, cache_path)
            second = get_or_create_profile(document, analyzer, cache_path)

        self.assertEqual(calls["count"], 1)
        self.assertEqual(first["content_hash"], second["content_hash"])
        self.assertEqual(second["content_summary"], "summary")

    def test_document_summary_export_writes_readable_files(self) -> None:
        record = {
            "document_code": "WI-01-A",
            "document_title": "Test procedure",
            "document_type": "WI",
            "file_name": "WI-01-A Test.docx",
            "file_path": "/tmp/WI-01-A Test.docx",
            "content_hash": "abc",
            "analyzed_at": "2026-07-03T00:00:00+00:00",
            "content_summary": "Readable cached summary.",
            "key_sections": [{"section_id": "P1", "location": "paragraph", "summary": "Purpose"}],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            paths = export_document_summaries([record], Path(tmpdir) / "summaries")
            summary_json = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
            summary_md = Path(paths["markdown"]).read_text(encoding="utf-8")

        self.assertEqual(summary_json[0]["content_summary"], "Readable cached summary.")
        self.assertIn("Readable cached summary.", summary_md)

    def test_document_cache_refreshes_same_filename_when_hash_changes(self) -> None:
        calls = {"count": 0}

        def analyzer(document: dict) -> dict:
            calls["count"] += 1
            return {"content_summary": f"summary {calls['count']}", "key_sections": []}

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "cache.json"
            get_or_create_profile(sample_document("WI-01-A Test.docx", "P1", "Original text"), analyzer, cache_path)
            refreshed = get_or_create_profile(sample_document("WI-01-A Test.docx", "P1", "Changed text"), analyzer, cache_path)

        self.assertEqual(calls["count"], 2)
        self.assertEqual(refreshed["content_summary"], "summary 2")

    def test_extracts_paragraph_section_3_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "QSP0723-A Test.docx"
            doc = Document()
            doc.add_paragraph("Purpose")
            doc.add_paragraph("Scope")
            doc.add_paragraph("References")
            doc.add_paragraph("Regulation (EU) 2017/745")
            doc.add_paragraph("MDCG 2019-9")
            doc.add_paragraph("Definition")
            doc.add_paragraph("No reference here")
            doc.save(path)

            extracted = extract_document(path)

        self.assertEqual(extracted["reference_regulations"], ["Regulation (EU) 2017/745"])
        self.assertEqual(extracted["reference_guidance"], ["MDCG 2019-9"])

    def test_extracts_table_section_3_references_in_document_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "WI-07-ENG-11-A Test.docx"
            doc = Document()
            doc.add_paragraph("Purpose")
            doc.add_paragraph("Scope")
            doc.add_paragraph("References")
            table = doc.add_table(rows=2, cols=2)
            table.cell(0, 0).text = "QSP0704"
            table.cell(0, 1).text = "Design and Development Control"
            table.cell(1, 0).text = "FM136"
            table.cell(1, 1).text = "Engineering Change Register"
            doc.add_paragraph("Definition")
            doc.add_paragraph("QSP9999 outside references")
            doc.save(path)

            extracted = extract_document(path)

        self.assertEqual(extracted["reference_documents"], ["QSP0704"])
        self.assertEqual(extracted["reference_forms"], ["FM136"])
        self.assertNotIn("QSP9999", extracted["reference_documents"])

    def test_prefers_explicit_3_0_reference_heading(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "QSP0701-A References.docx"
            doc = Document()
            doc.add_paragraph("References")
            doc.add_paragraph("QSP9999 legacy reference")
            doc.add_paragraph("1.0 Purpose")
            doc.add_paragraph("2.0 Scope")
            doc.add_paragraph("3.0 References")
            doc.add_paragraph("QSP0704")
            doc.add_paragraph("4.0 Definition")
            doc.save(path)

            extracted = extract_document(path)

        self.assertEqual(extracted["reference_documents"], ["QSP0704"])
        self.assertEqual(extracted["reference_detection_source"], "plain_text_reference_block")

    def test_falls_back_to_first_reference_heading(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "QSP0702-A References.docx"
            doc = Document()
            doc.add_paragraph("Purpose")
            doc.add_paragraph("References")
            doc.add_paragraph("QSP0704")
            doc.add_paragraph("Definition")
            doc.save(path)

            extracted = extract_document(path)

        self.assertEqual(extracted["reference_documents"], ["QSP0704"])
        self.assertEqual(extracted["reference_detection_source"], "plain_text_reference_block")

    def test_manual_section3_annotation_overrides_automatic_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "QSP0703-A Manual.docx"
            doc = Document()
            doc.add_paragraph("Purpose")
            doc.add_paragraph("3.0 References")
            doc.add_paragraph("QSP9999 automatic reference")
            doc.add_paragraph("4.0 Definition")
            doc.add_paragraph("QSP0704 manual reference")
            doc.save(path)
            sections = extract_docx_sections(path)
            main_sections = extract_main_sections(sections)
            annotation_path = root / "annotations.json"
            annotation_path.write_text(
                json.dumps(
                    {
                        str(path.resolve()): {
                            "file_path": str(path.resolve()),
                            "file_name": path.name,
                            "document_code": "QSP0703",
                            "start_section_id": "P5",
                            "end_section_id": "P5",
                            "annotated_reference_text": "QSP0704 manual reference",
                        }
                    }
                ),
                encoding="utf-8",
            )

            references = extract_section_references(
                sections,
                main_sections,
                file_path=path,
                document_code="QSP0703",
                annotation_path=annotation_path,
            )

        self.assertEqual(references["reference_documents"], ["QSP0704"])
        self.assertEqual(references["reference_detection_source"], "manual_annotation")
        self.assertFalse(references["reference_annotation_stale"])

    def test_stale_manual_section3_annotation_falls_back_to_auto(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "QSP0705-A Stale.docx"
            doc = Document()
            doc.add_paragraph("Purpose")
            doc.add_paragraph("3.0 References")
            doc.add_paragraph("QSP0704")
            doc.add_paragraph("4.0 Definition")
            doc.save(path)
            sections = extract_docx_sections(path)
            main_sections = extract_main_sections(sections)
            annotation_path = root / "annotations.json"
            annotation_path.write_text(
                json.dumps(
                    {
                        str(path.resolve()): {
                            "file_path": str(path.resolve()),
                            "file_name": path.name,
                            "document_code": "QSP0705",
                            "start_section_id": "missing-start",
                            "end_section_id": "missing-end",
                            "annotated_reference_text": "",
                        }
                    }
                ),
                encoding="utf-8",
            )

            references = extract_section_references(
                sections,
                main_sections,
                file_path=path,
                document_code="QSP0705",
                annotation_path=annotation_path,
            )

        self.assertEqual(references["reference_documents"], ["QSP0704"])
        self.assertEqual(references["reference_detection_source"], "deterministic_section3")
        self.assertTrue(references["reference_annotation_stale"])

    def test_deterministic_section3_extracts_chinese_references_document(self) -> None:
        sections = [
            section("P1", "History of Change Modified Section 3.0 for References Documents QSP9999"),
            section("P2", "1.0 Purpose"),
            section("P3", "2.0 Scope"),
            section("P4", "3.0 参考文件 References Document QSP0807 纠正及预防措施 Corrective and Preventive Action 4.0 定义 Definition"),
        ]

        references = extract_section_references(sections, [])

        self.assertTrue(references["section3_found"])
        self.assertEqual(references["reference_documents"], ["QSP0807"])
        self.assertNotIn("QSP9999", references["reference_documents"])

    def test_deterministic_section3_extracts_numbered_qsp_items(self) -> None:
        sections = [
            section("P1", "1.0 目的"),
            section("P2", "2.0 范围"),
            section("P3", "3.0 参考文件 References Document"),
            section("P4", "3.1 QSP0715 设备管理、维修及保养"),
            section("P5", "3.2 QSP0716"),
            section("P6", "4.0 定义"),
        ]

        references = extract_section_references(sections, [])

        self.assertEqual(references["reference_documents"], ["QSP0715", "QSP0716"])

    def test_deterministic_section3_separates_internal_and_external_references(self) -> None:
        sections = [
            section("P1", "3.0 References QSP0804 Non-Conforming Control ISO11137 Sterilization AAMI TIR33 4.0 Definition"),
        ]

        references = extract_section_references(sections, [])

        self.assertEqual(references["reference_documents"], ["QSP0804"])
        self.assertIn("ISO11137", references["reference_standards"])
        self.assertIn("AAMI TIR33", references["reference_standards"])

    def test_marker_reference_document_matches_qsp_original_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            original_dir = root / "original file"
            original_dir.mkdir()
            matched_file = original_dir / "QSP0715-A 设备的管理维修及保养.docx"
            matched_file.write_text("", encoding="utf-8")
            sections = [
                {"section_id": "P1", "page_or_location": "paragraph", "text": "參考文件 Reference Document", "contains_regulation_reference": False},
                {"section_id": "P2", "page_or_location": "paragraph", "text": "QSP0715 设备的管理、维修及保养", "contains_regulation_reference": False},
            ]
            main_sections = [{"section_id": "P1", "number": "3.0", "title": "References", "start_index": "0"}]

            references = extract_section_references(sections, main_sections, original_file_dir=original_dir)

        self.assertTrue(references["reference_document_marker_found"])
        self.assertEqual(references["reference_documents"], ["QSP0715"])
        self.assertEqual(references["matched_reference_files"][0]["document_code"], "QSP0715")
        self.assertEqual(references["matched_reference_files"][0]["file_name"], matched_file.name)

    def test_reference_items_preserve_names_and_match_original_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            original_dir = root / "original file"
            original_dir.mkdir()
            qsp_file = original_dir / "QSP0704-N DESIGN AND DEVELOPMENT CONTROL.docx"
            wi_file = original_dir / "WI-04-QAD-01-J  DOCUMENT NUMBERING INSTRUCTIONS文件编码指引.doc"
            qsp_file.write_text("", encoding="utf-8")
            wi_file.write_text("", encoding="utf-8")
            sections = [
                section("P1", "1.0 Purpose"),
                section("P2", "3.0 参考文件 References Document"),
                section("P3", "QSP0704 设计与开发控制及新产品引进控制 Design and Development Control"),
                section("P4", "WI-04-QAD-01 文件编码指引Document Numbering Instruction"),
                section("P5", "4.0 Definition"),
            ]

            references = extract_section_references(sections, [], original_file_dir=original_dir)

        items = {item["reference_code"]: item for item in references["reference_items"]}
        self.assertEqual(items["QSP0704"]["reference_name"], "设计与开发控制及新产品引进控制 Design and Development Control")
        self.assertEqual(items["WI-04-QAD-01"]["reference_name"], "文件编码指引Document Numbering Instruction")
        self.assertEqual(items["QSP0704"]["match_status"], "matched")
        self.assertEqual(items["WI-04-QAD-01"]["matched_files"][0]["file_name"], wi_file.name)

    def test_reference_heading_ignores_history_and_uses_real_section(self) -> None:
        sections = [
            section("P1", "History of Change Modified Section 3.0 for References Documents QSP9999"),
            section("P2", "1.0 Purpose"),
            section("P3", "2.0 Scope"),
            section("P4", "3.0 References"),
            section("P5", "QSP0807 Corrective and Preventive Action"),
            section("P6", "4.0 Definition"),
        ]

        references = extract_section_references(sections, [])

        self.assertEqual(references["reference_documents"], ["QSP0807"])
        self.assertNotIn("QSP9999", references["reference_documents"])

    def test_marker_reference_updates_detected_section_range(self) -> None:
        sections = [
            {"section_id": "P1", "page_or_location": "paragraph", "text": "3.0 References", "contains_regulation_reference": False},
            {"section_id": "P2", "page_or_location": "paragraph", "text": "Legacy heading text", "contains_regulation_reference": False},
            {"section_id": "P3", "page_or_location": "paragraph", "text": "參考文件 References", "contains_regulation_reference": False},
            {"section_id": "P4", "page_or_location": "paragraph", "text": "QSP0715 设备的管理、维修及保养", "contains_regulation_reference": False},
            {"section_id": "P5", "page_or_location": "paragraph", "text": "4.0 Definition", "contains_regulation_reference": False},
        ]
        main_sections = [
            {"section_id": "P1", "number": "3.0", "title": "References", "start_index": "0"},
            {"section_id": "P5", "number": "4.0", "title": "Definition", "start_index": "4"},
        ]

        references = extract_section_references(sections, main_sections)

        self.assertEqual(references["reference_detection_source"], "deterministic_section3")
        self.assertEqual(references["reference_section_start_id"], "P1")
        self.assertEqual(references["reference_section_end_id"], "P4")
        self.assertEqual(references["reference_documents"], ["QSP0715"])

    def test_marker_reference_document_matches_wi_original_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            original_dir = root / "original file"
            original_dir.mkdir()
            matched_file = original_dir / "WI-07-PRD-01-B 上下模及模具维护.doc"
            matched_file.write_text("", encoding="utf-8")
            sections = [
                {"section_id": "P1", "page_or_location": "paragraph", "text": "參考文件 Reference Document", "contains_regulation_reference": False},
                {"section_id": "P2", "page_or_location": "paragraph", "text": "WI-07-PRD-01 上下模及模具维护", "contains_regulation_reference": False},
            ]
            main_sections = [{"section_id": "P1", "number": "3.0", "title": "References", "start_index": "0"}]

            references = extract_section_references(sections, main_sections, original_file_dir=original_dir)

        self.assertEqual(references["reference_documents"], ["WI-07-PRD-01"])
        self.assertEqual(references["matched_reference_files"][0]["document_code"], "WI-07-PRD-01")
        self.assertEqual(references["matched_reference_files"][0]["file_name"], matched_file.name)

    def test_marker_reference_variants_match_original_files(self) -> None:
        markers = ["參考文件", "參考文件 References Document", "參考文件 References"]
        for marker in markers:
            with self.subTest(marker=marker):
                with tempfile.TemporaryDirectory() as tmpdir:
                    root = Path(tmpdir)
                    original_dir = root / "original file"
                    original_dir.mkdir()
                    matched_file = original_dir / "QSP0715-A 设备的管理维修及保养.docx"
                    matched_file.write_text("", encoding="utf-8")
                    sections = [
                        {"section_id": "P1", "page_or_location": "paragraph", "text": marker, "contains_regulation_reference": False},
                        {"section_id": "P2", "page_or_location": "paragraph", "text": "QSP0715 设备的管理、维修及保养", "contains_regulation_reference": False},
                    ]
                    main_sections = [{"section_id": "P1", "number": "3.0", "title": "References", "start_index": "0"}]

                    references = extract_section_references(sections, main_sections, original_file_dir=original_dir)

                self.assertTrue(references["reference_document_marker_found"])
                self.assertEqual(references["reference_document_marker"], marker)
                self.assertEqual(references["reference_documents"], ["QSP0715"])
                self.assertEqual(references["matched_reference_files"][0]["file_name"], matched_file.name)

    def test_marker_reference_document_retains_unmatched_reference_for_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            original_dir = Path(tmpdir) / "original file"
            original_dir.mkdir()
            sections = [
                {"section_id": "P1", "page_or_location": "paragraph", "text": "參考文件 Reference Document", "contains_regulation_reference": False},
                {"section_id": "P2", "page_or_location": "paragraph", "text": "QSP9999 Missing reference", "contains_regulation_reference": False},
            ]
            main_sections = [{"section_id": "P1", "number": "3.0", "title": "References", "start_index": "0"}]

            references = extract_section_references(sections, main_sections, original_file_dir=original_dir)

        self.assertEqual(references["matched_reference_files"], [])
        self.assertEqual(references["unmatched_reference_files"], ["QSP9999"])

    def test_graph_resolves_reference_file_paths(self) -> None:
        source = sample_document(
            "QSP0703-A Source.docx",
            "P1",
            "Source",
            code="QSP0703",
            references={"reference_documents": ["QSP0716"]},
        )
        target = sample_document("QSP0716-A Target.docx", "P1", "Target document", code="QSP0716")

        graph = attach_reference_matches([source, target])

        self.assertEqual(graph["forward"]["QSP0703"], ["QSP0716"])
        self.assertEqual(graph["reverse"]["QSP0716"], ["QSP0703"])
        self.assertEqual(source["matched_reference_files"][0]["file_path"], "/tmp/QSP0716-A Target.docx")

    def test_graph_references_produce_indirect_candidate_rows(self) -> None:
        regulations = pd.DataFrame(
            [{"原标准编号": "ISO 14971:2019", "最新版本": "ISO 14971:2025", "最新年份": 2025, "状态": "已更新"}]
        )
        direct = sample_document(
            "QSP0716-A Target.docx",
            "P1",
            "Reference ISO 14971",
            code="QSP0716",
            references={"reference_standards": ["ISO14971"]},
        )
        source = sample_document(
            "QSP0703-A Source.docx",
            "P1",
            "Source",
            code="QSP0703",
            references={"reference_documents": ["QSP0716"]},
        )

        result = select_candidate_pairs(regulations, [source, direct])
        by_code = {row.document_code: row for row in result.itertuples()}

        self.assertEqual(by_code["QSP0716"].candidate_level, "Level 2 - Direct standard/guidance reference")
        self.assertEqual(by_code["QSP0703"].candidate_level, "Level 3 - Referenced QSP/WI relationship")
        self.assertTrue(by_code["QSP0703"].review_required)

    def test_unresolved_internal_reference_is_audit_only(self) -> None:
        source = sample_document(
            "QSP0703-A Source.docx",
            "P1",
            "Source",
            code="QSP0703",
            references={"reference_documents": ["QSP9999"]},
        )

        graph = attach_reference_matches([source])

        self.assertEqual(graph["unresolved"][0]["referenced_code"], "QSP9999")
        self.assertEqual(source["matched_reference_files"], [])

    def test_reference_map_rows_include_matched_unresolved_and_no_reference_files(self) -> None:
        matched = sample_document("QSP0703-A Source.docx", "P1", "Source", code="QSP0703")
        matched["reference_items"] = [
            {
                "reference_code": "QSP0716",
                "reference_name": "Calibration Control",
                "reference_text": "QSP0716 Calibration Control",
                "match_status": "matched",
                "matched_files": [{"document_code": "QSP0716", "file_name": "QSP0716-A Target.docx", "file_path": "/tmp/QSP0716-A Target.docx"}],
            }
        ]
        unresolved = sample_document("QSP0704-A Source.docx", "P1", "Source", code="QSP0704")
        unresolved["reference_items"] = [
            {
                "reference_code": "QSP9999",
                "reference_name": "Missing",
                "reference_text": "QSP9999 Missing",
                "match_status": "unresolved",
                "matched_files": [],
            }
        ]
        no_reference = sample_document("QSP0705-A Source.docx", "P1", "Source", code="QSP0705")
        no_reference["reference_items"] = []

        rows = build_reference_map_rows([matched, unresolved, no_reference])
        statuses = [row["match_status"] for row in rows]

        self.assertEqual(statuses, ["matched", "unresolved", "no_internal_reference"])
        self.assertEqual(rows[0]["reference_name"], "Calibration Control")
        self.assertEqual(rows[0]["matched_file"], "QSP0716-A Target.docx")

    def test_generic_only_topic_match_is_weak_not_ai_candidate(self) -> None:
        regulations = pd.DataFrame(
            [{"原标准编号": "ISO 9999:2000", "最新版本": "ISO 9999:2020", "最新年份": 2020, "状态": "已更新"}]
        )
        profiles = [{"affected_qms_topics": ["control"], "search_keywords": ["document", "control"]}]
        documents = [sample_document("QSP0001-A Control.docx", "P1", "Document control procedure.", code="QSP0001")]

        result = select_candidate_pairs(regulations, documents, amendment_profiles=profiles)
        row = result.iloc[0]

        self.assertEqual(row["candidate_level"], "Level 5 - Weak keyword match only")
        self.assertFalse(row["review_required"])

    def test_file_regulation_pairing_uses_section3_reference_for_updated_rows(self) -> None:
        regulations = pd.DataFrame(
            [
                {"原标准编号": "BS EN ISO 14971", "最新版本": "ISO 14971:2019", "状态": "已更新", "主要变更内容": "风险管理标准更新"},
                {"原标准编号": "ISO 13485", "最新版本": "ISO 13485:2016", "状态": "当前版本", "主要变更内容": "无更新"},
            ]
        )
        document = sample_document(
            "QSP0720-J RISK MANAGEMENT 风险管理.docx",
            "P1",
            "Risk management procedure.",
            code="QSP0720",
            references={"reference_standards": ["ISO14971"]},
        )

        result = build_file_regulation_pairing_map(regulations, [document], amendment_profiles=[{}, {}])

        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["match_method"], "section3_reference")
        self.assertEqual(result.iloc[0]["confidence"], "High")
        self.assertFalse(bool(result.iloc[0]["review_required"]))

    def test_file_regulation_pairing_uses_chinese_filename_profile_evidence(self) -> None:
        regulations = pd.DataFrame(
            [{"原标准编号": "BS EN ISO 14971", "最新版本": "ISO 14971:2019", "状态": "已更新", "主要变更内容": "医疗器械风险管理标准更新"}]
        )
        document = sample_document(
            "QSP0720-J RISK MANAGEMENT 风险管理.docx",
            "P1",
            "Procedure text without standard number.",
            code="QSP0720",
        )
        profiles = [{"affected_qms_topics": ["风险管理"], "search_keywords": ["risk management"]}]

        result = build_file_regulation_pairing_map(regulations, [document], amendment_profiles=profiles)

        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["match_method"], "chinese_filename")
        self.assertEqual(result.iloc[0]["confidence"], "Medium")
        self.assertEqual(result.iloc[0]["source_field"], "amendment_profile")
        self.assertFalse(bool(result.iloc[0]["review_required"]))

    def test_file_regulation_pairing_does_not_duplicate_section3_and_filename_match(self) -> None:
        regulations = pd.DataFrame(
            [{"原标准编号": "BS EN ISO 14971", "最新版本": "ISO 14971:2019", "状态": "已更新", "主要变更内容": "医疗器械风险管理标准更新"}]
        )
        document = sample_document(
            "QSP0720-J RISK MANAGEMENT 风险管理.docx",
            "P1",
            "Risk management procedure.",
            code="QSP0720",
            references={"reference_standards": ["ISO14971"]},
        )
        profiles = [{"affected_qms_topics": ["风险管理"], "search_keywords": []}]

        result = build_file_regulation_pairing_map(regulations, [document], amendment_profiles=profiles)

        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["match_method"], "section3_reference")

    def test_file_regulation_pairing_ignores_generic_chinese_filename_terms(self) -> None:
        regulations = pd.DataFrame(
            [{"原标准编号": "ISO 13485", "最新版本": "ISO 13485:2016", "状态": "已更新", "主要变更内容": "质量管理体系标准更新"}]
        )
        document = sample_document(
            "QSP0401-P DOCUMENT AND RECORD CONTROL文件记录控制.docx",
            "P1",
            "Document control procedure.",
            code="QSP0401",
        )
        profiles = [{"affected_qms_topics": ["质量管理体系"], "search_keywords": ["文件", "记录", "控制"]}]

        result = build_file_regulation_pairing_map(regulations, [document], amendment_profiles=profiles)

        self.assertTrue(result.empty)

    def test_file_regulation_pairing_dataframe_can_export_to_xlsx(self) -> None:
        regulations = pd.DataFrame(
            [{"原标准编号": "BS EN ISO 14971", "最新版本": "ISO 14971:2019", "状态": "已更新", "主要变更内容": "风险管理标准更新"}]
        )
        document = sample_document(
            "QSP0720-J RISK MANAGEMENT 风险管理.docx",
            "P1",
            "Risk management procedure.",
            code="QSP0720",
            references={"reference_standards": ["ISO14971"]},
        )

        result = build_file_regulation_pairing_map(regulations, [document])
        payload = dataframe_to_excel_bytes(result)

        self.assertGreater(len(payload), 100)
        self.assertIn("original_standard_code", result.columns)

    def test_dataframe_can_export_to_csv_bytes(self) -> None:
        result = pd.DataFrame([{"document_code": "QSP0401", "regulation_old": "ISO 14971"}])
        payload = dataframe_to_csv_bytes(result)

        self.assertTrue(payload.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"document_code,regulation_old", payload)
        self.assertIn(b"QSP0401,ISO 14971", payload)

    def test_reference_tree_groups_regulations_internal_refs_and_unknowns(self) -> None:
        regulations = pd.DataFrame(
            [{"原标准编号": "ISO 14971:2019", "最新版本": "ISO 14971:2025", "状态": "已更新"}]
        )
        source = sample_document(
            "WI-01-A Source.docx",
            "P1",
            "Procedure references ISO 14971 and QSP0703.",
            code="WI-01-A",
            references={"reference_standards": ["ISO 14971"], "reference_documents": ["QSP0703"]},
        )
        source["reference_items"] = [
            {
                "reference_code": "QSP0703",
                "matched_files": [{"document_code": "QSP0703", "file_name": "QSP0703-A Target.docx", "file_path": "/tmp/QSP0703-A Target.docx"}],
            },
            {"reference_code": "UNKNOWN-1", "matched_files": []},
        ]

        tree = build_reference_tree(regulations, [source])

        self.assertEqual(tree["regulations"][0]["children"][0]["document_code"], "WI-01-A")
        self.assertEqual(tree["controlled_documents"][0]["children"][0]["document_code"], "QSP0703")
        self.assertEqual(tree["unknown"][0]["children"][0]["document_code"], "WI-01-A")

    def test_chinese_only_cleans_bilingual_header_and_preserves_qsp_code(self) -> None:
        cleaned, codes = clean_chinese_only_line("质量管理体系 Quality Management System 文件编号 Doc No.： QSP0706")

        self.assertEqual(cleaned, "质量管理体系 文件编号： QSP0706")
        self.assertEqual(codes, ["QSP0706"])

    def test_chinese_only_preserves_qsp_line_and_removes_english_title(self) -> None:
        cleaned, codes = clean_chinese_only_line(
            "3.1 QSP0715 设备的管理、维修及保养 Management, Repair and Maintenance of Equipment"
        )

        self.assertEqual(cleaned, "3.1 QSP0715 设备的管理、维修及保养")
        self.assertEqual(codes, ["QSP0715"])

    def test_chinese_only_preserves_wi_line_and_removes_english_title(self) -> None:
        cleaned, codes = clean_chinese_only_line(
            "3.7 WI-07-PRD-01 上/下模及模具维护指引 Mounting, De-mounting and Maintenance of Mold Instruction"
        )

        self.assertEqual(cleaned, "3.7 WI-07-PRD-01 上/下模及模具维护指引")
        self.assertEqual(codes, ["WI-07-PRD-01"])

    def test_chinese_only_discards_english_only_headings(self) -> None:
        self.assertEqual(clean_chinese_only_line("Procedure Name:")[0], "")
        self.assertEqual(clean_chinese_only_line("Purchasing and Outsourcing Process")[0], "")

    def test_chinese_only_keeps_code_from_english_heavy_line(self) -> None:
        cleaned, codes = clean_chinese_only_line("WI-07-PRD-01 Mounting, De-mounting and Maintenance of Mold Instruction")

        self.assertEqual(cleaned, "WI-07-PRD-01")
        self.assertEqual(codes, ["WI-07-PRD-01"])

    def test_chinese_only_preserves_chinese_sentence_with_wi_code(self) -> None:
        cleaned, codes = clean_chinese_only_line("技术员根据 WI-07-PRD-02 (烤料/配料指引)配料、烤料（如有需要）并作记录；")

        self.assertEqual(cleaned, "技术员根据 WI-07-PRD-02 (烤料/配料指引)配料、烤料（如有需要）并作记录；")
        self.assertEqual(codes, ["WI-07-PRD-02"])

    def test_chinese_only_table_cells_drop_english_only_cells(self) -> None:
        cleaned, codes, reason = extract_chinese_only_text(
            "Code | 名称 | Stored at | QSP0721 物料的计划与控制",
            separator=" | ",
        )

        self.assertEqual(cleaned, "名称 | QSP0721 物料的计划与控制")
        self.assertEqual(codes, ["QSP0721"])
        self.assertEqual(reason, "kept_chinese_and_controlled_code")

    def test_candidate_selection_uses_direct_indirect_topic_and_skip_levels(self) -> None:
        regulations = pd.DataFrame(
            [{"原标准编号": "ISO 14971:2019", "最新版本": "ISO 14971:2025", "最新年份": 2025, "状态": "已更新"}]
        )
        documents = [
            sample_document(
                "QSP0720-A Risk.docx",
                "P1",
                "References ISO 14971",
                code="QSP0720",
                references={"reference_standards": ["ISO 14971"]},
            ),
            sample_document(
                "WI-01-A Linked.docx",
                "P1",
                "Work instruction",
                code="WI-01-A",
                references={"reference_documents": ["QSP0720"]},
            ),
            sample_document("WI-02-A Topic.docx", "P1", "Procedure applies ISO 14971 controls.", code="WI-02-A"),
            sample_document("WI-03-A Weak.docx", "P1", "风险管理 is mentioned once.", code="WI-03-A"),
            sample_document("WI-04-A Other.docx", "P1", "Packaging instruction.", code="WI-04-A"),
        ]

        result = select_candidate_pairs(regulations, documents)

        by_code = {row.document_code: row for row in result.itertuples()}
        self.assertEqual(by_code["QSP0720"].candidate_level, "Level 2 - Direct standard/guidance reference")
        self.assertEqual(by_code["WI-01-A"].candidate_level, "Level 3 - Referenced QSP/WI relationship")
        self.assertEqual(by_code["WI-02-A"].candidate_level, "Level 4 - Topic/category match")
        self.assertFalse(by_code["WI-03-A"].review_required)
        self.assertEqual(by_code["WI-04-A"].candidate_level, "Skipped")
        self.assertEqual(int(result["review_required"].sum()), 3)

    def test_impact_analyzer_reviews_only_selected_candidates(self) -> None:
        regulations = pd.DataFrame(
            [{"原标准编号": "ISO 14971:2019", "最新版本": "ISO 14971:2025", "最新年份": 2025, "状态": "已更新"}]
        )
        documents = [
            sample_document("WI-01-A Test.docx", "P1", "Procedure applies ISO 14971 controls.", code="WI-01-A"),
            sample_document("WI-02-A Other.docx", "P1", "Operating instruction without a standard number.", code="WI-02-A"),
        ]
        review_inputs = []

        def analyzer(_: dict) -> dict:
            return {"content_summary": "Risk control activity summary", "key_sections": [{"section_id": "P1"}]}

        def reviewer(input_data: dict) -> dict:
            review_inputs.append(input_data)
            return {
                "ai_decision": "Affected",
                "risk_level": "Medium",
                "reason": "Document activity is affected.",
                "recommended_amendment_locations": [{"section_id": "P1", "location": "paragraph", "reason": "Update control step."}],
                "supporting_evidence": "AI document-level review",
            }

        with tempfile.TemporaryDirectory() as tmpdir:
            result = analyze_impact(regulations, documents, Path(tmpdir) / "cache.json", analyzer, reviewer)

        self.assertEqual(len(result), 1)
        self.assertEqual(len(review_inputs), 1)
        self.assertEqual(review_inputs[0]["candidate_level"], "Level 4 - Topic/category match")
        self.assertEqual(result.iloc[0]["ai_decision"], "Affected")
        self.assertEqual(result.iloc[0]["section_id"], "P1")
        self.assertEqual(result.iloc[0]["candidate_level"], "Level 4 - Topic/category match")

    def test_impact_analyzer_uses_amendment_profile_topic_context(self) -> None:
        regulations = pd.DataFrame(
            [
                {
                    "原标准编号": "ISO 9999:2000",
                    "最新版本": "ISO 9999:2020",
                    "最新年份": 2020,
                    "状态": "已更新",
                    "主要变更内容": "Excel summary says packaging validation requirements changed.",
                }
            ]
        )
        documents = [sample_document("WI-02-A Packaging.docx", "P1", "Sterile barrier packaging validation procedure.", code="WI-02-A")]
        profiles = [
            {
                "source_mode": "excel_amendment_summary",
                "confidence": "Medium",
                "amendment_summary_used": "Excel summary says packaging validation requirements changed.",
                "exact_clause_verified": False,
                "needs_human_confirmation": True,
                "limitations": ["Amendment profile is based on Excel summary."],
                "affected_qms_topics": ["packaging validation"],
                "search_keywords": ["packaging", "sterile barrier"],
            }
        ]
        review_inputs = []

        def analyzer(_: dict) -> dict:
            return {"content_summary": "Packaging validation", "key_sections": [{"section_id": "P1"}]}

        def reviewer(input_data: dict) -> dict:
            review_inputs.append(input_data)
            return {
                "ai_decision": "Affected",
                "risk_level": "Medium",
                "reason": "Packaging validation is affected.",
                "recommended_amendment_locations": [],
                "supporting_evidence": "Profile topic match",
            }

        with tempfile.TemporaryDirectory() as tmpdir:
            result = analyze_impact(regulations, documents, Path(tmpdir) / "cache.json", analyzer, reviewer, profiles)

        self.assertEqual(len(result), 1)
        self.assertEqual(review_inputs[0]["amendment_source_mode"], "excel_amendment_summary")
        self.assertEqual(result.iloc[0]["amendment_source_mode"], "excel_amendment_summary")
        self.assertEqual(result.iloc[0]["candidate_level"], "Level 4 - Topic/category match")

    def test_review_one_candidate_matches_analyze_impact_row_shape(self) -> None:
        regulations = pd.DataFrame(
            [{"原标准编号": "ISO 14971:2019", "最新版本": "ISO 14971:2025", "最新年份": 2025, "状态": "已更新"}]
        )
        document = sample_document("WI-01-A Test.docx", "P1", "Procedure applies ISO 14971 controls.", code="WI-01-A")
        candidates = select_candidate_pairs(regulations, [document])
        candidate = candidates[candidates["review_required"] == True].iloc[0]  # noqa: E712

        def analyzer(_: dict) -> dict:
            return {"content_summary": "Risk control activity summary", "key_sections": [{"section_id": "P1"}]}

        def reviewer(_: dict) -> dict:
            return {
                "ai_decision": "Affected",
                "risk_level": "Medium",
                "reason": "Document activity is affected.",
                "recommended_amendment_locations": [{"section_id": "P1", "location": "paragraph"}],
                "supporting_evidence": "AI document-level review",
            }

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "cache.json"
            single = review_one_candidate(candidate, document, cache_path, analyzer, reviewer)
            full = analyze_impact(regulations, [document], cache_path, analyzer, reviewer)

        self.assertIsNotNone(single)
        self.assertEqual(single["document_code"], full.iloc[0]["document_code"])
        self.assertEqual(single["ai_decision"], full.iloc[0]["ai_decision"])
        self.assertEqual(single["candidate_level"], full.iloc[0]["candidate_level"])

    def test_incremental_job_writes_completed_result_after_first_review(self) -> None:
        regulations = pd.DataFrame(
            [{"原标准编号": "ISO 14971:2019", "最新版本": "ISO 14971:2025", "最新年份": 2025, "状态": "已更新"}]
        )
        document = sample_document("WI-01-A Test.docx", "P1", "Procedure applies ISO 14971 controls.", code="WI-01-A")
        candidates = select_candidate_pairs(regulations, [document])

        with tempfile.TemporaryDirectory() as tmpdir:
            job_dir = Path(tmpdir) / "jobs"
            job_id = create_review_job(candidates, [document], job_dir=job_dir)
            run_review_job(job_id, [document], Path(tmpdir) / "cache.json", fake_analyzer, fake_affected_reviewer, job_dir=job_dir, max_candidates=1)
            results = load_review_results(job_id, job_dir)
            job = load_review_job(job_id, job_dir)

        self.assertEqual(len(results), 1)
        self.assertEqual(job["completed_count"], 1)
        self.assertEqual(job["affected_count"], 1)
        self.assertEqual(job["status"], "stopped")

    def test_incremental_job_advances_not_affected_without_result_row(self) -> None:
        regulations = pd.DataFrame(
            [{"原标准编号": "ISO 14971:2019", "最新版本": "ISO 14971:2025", "最新年份": 2025, "状态": "已更新"}]
        )
        document = sample_document("WI-01-A Test.docx", "P1", "Procedure applies ISO 14971 controls.", code="WI-01-A")
        candidates = select_candidate_pairs(regulations, [document])

        def not_affected_reviewer(_: dict) -> dict:
            return {
                "ai_decision": "Not affected",
                "risk_level": "Low",
                "reason": "Not affected.",
                "recommended_amendment_locations": [],
                "supporting_evidence": "",
            }

        with tempfile.TemporaryDirectory() as tmpdir:
            job_dir = Path(tmpdir) / "jobs"
            job_id = create_review_job(candidates, [document], job_dir=job_dir)
            run_review_job(job_id, [document], Path(tmpdir) / "cache.json", fake_analyzer, not_affected_reviewer, job_dir=job_dir)
            results = load_review_results(job_id, job_dir)
            job = load_review_job(job_id, job_dir)

        self.assertTrue(results.empty)
        self.assertEqual(job["completed_count"], 1)
        self.assertEqual(job["affected_count"], 0)
        self.assertEqual(job["status"], "completed")

    def test_incremental_job_saves_reviewer_decisions_separately(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            job_dir = Path(tmpdir) / "jobs"
            job_id = create_review_job(pd.DataFrame([sample_candidate_row()]), job_dir=job_dir)
            save_reviewer_decision(job_id, "row-1", "Revise", "Clarify evidence.", job_dir=job_dir)
            decisions = load_reviewer_decisions(job_id, job_dir)

        self.assertEqual(decisions["row-1"]["decision"], "Revise")
        self.assertEqual(decisions["row-1"]["comment"], "Clarify evidence.")

    def test_incremental_job_resume_skips_completed_candidates(self) -> None:
        regulations = pd.DataFrame(
            [{"原标准编号": "ISO 14971:2019", "最新版本": "ISO 14971:2025", "最新年份": 2025, "状态": "已更新"}]
        )
        documents = [
            sample_document("WI-01-A Test.docx", "P1", "Procedure applies ISO 14971 controls.", code="WI-01-A"),
            sample_document("WI-02-A Test.docx", "P1", "Procedure applies ISO 14971 controls.", code="WI-02-A"),
        ]
        candidates = select_candidate_pairs(regulations, documents)
        reviewed_codes = []

        def reviewer(input_data: dict) -> dict:
            reviewed_codes.append(input_data["document_code"])
            return fake_affected_reviewer(input_data)

        with tempfile.TemporaryDirectory() as tmpdir:
            job_dir = Path(tmpdir) / "jobs"
            job_id = create_review_job(candidates, documents, job_dir=job_dir)
            run_review_job(job_id, documents, Path(tmpdir) / "cache.json", fake_analyzer, reviewer, job_dir=job_dir, max_candidates=1)
            run_review_job(job_id, documents, Path(tmpdir) / "cache.json", fake_analyzer, reviewer, job_dir=job_dir)
            results = load_review_results(job_id, job_dir)
            job = load_review_job(job_id, job_dir)

        self.assertEqual(len(results), 2)
        self.assertEqual(job["completed_count"], 2)
        self.assertEqual(reviewed_codes, ["WI-01-A", "WI-02-A"])

    def test_incremental_job_error_remains_resumable(self) -> None:
        regulations = pd.DataFrame(
            [{"原标准编号": "ISO 14971:2019", "最新版本": "ISO 14971:2025", "最新年份": 2025, "状态": "已更新"}]
        )
        document = sample_document("WI-01-A Test.docx", "P1", "Procedure applies ISO 14971 controls.", code="WI-01-A")
        candidates = select_candidate_pairs(regulations, [document])
        calls = {"count": 0}

        def flaky_reviewer(input_data: dict) -> dict:
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("temporary AI failure")
            return fake_affected_reviewer(input_data)

        with tempfile.TemporaryDirectory() as tmpdir:
            job_dir = Path(tmpdir) / "jobs"
            job_id = create_review_job(candidates, [document], job_dir=job_dir)
            run_review_job(job_id, [document], Path(tmpdir) / "cache.json", fake_analyzer, flaky_reviewer, job_dir=job_dir)
            first_job = load_review_job(job_id, job_dir)
            run_review_job(job_id, [document], Path(tmpdir) / "cache.json", fake_analyzer, flaky_reviewer, job_dir=job_dir)
            results = load_review_results(job_id, job_dir)
            final_job = load_review_job(job_id, job_dir)

        self.assertEqual(first_job["completed_count"], 0)
        self.assertEqual(first_job["affected_count"], 0)
        self.assertEqual(first_job["status"], "stopped")
        self.assertEqual(len(results), 1)
        self.assertEqual(final_job["completed_count"], 1)
        self.assertEqual(final_job["affected_count"], 1)
        self.assertEqual(final_job["status"], "completed")

    def test_incremental_job_stop_prevents_next_candidate(self) -> None:
        regulations = pd.DataFrame(
            [{"原标准编号": "ISO 14971:2019", "最新版本": "ISO 14971:2025", "最新年份": 2025, "状态": "已更新"}]
        )
        documents = [
            sample_document("WI-01-A Test.docx", "P1", "Procedure applies ISO 14971 controls.", code="WI-01-A"),
            sample_document("WI-02-A Test.docx", "P1", "Procedure applies ISO 14971 controls.", code="WI-02-A"),
        ]
        candidates = select_candidate_pairs(regulations, documents)
        reviewed_codes = []

        with tempfile.TemporaryDirectory() as tmpdir:
            job_dir = Path(tmpdir) / "jobs"
            job_id = create_review_job(candidates, documents, job_dir=job_dir)

            def reviewer(input_data: dict) -> dict:
                reviewed_codes.append(input_data["document_code"])
                request_stop_review_job(job_id, job_dir=job_dir)
                return fake_affected_reviewer(input_data)

            run_review_job(job_id, documents, Path(tmpdir) / "cache.json", fake_analyzer, reviewer, job_dir=job_dir)
            job = load_review_job(job_id, job_dir)

        self.assertEqual(reviewed_codes, ["WI-01-A"])
        self.assertEqual(job["status"], "stopped")
        self.assertEqual(job["completed_count"], 1)

    def test_agentic_amendment_creates_docx_draft_without_overwriting_original(self) -> None:
        original_impact = amendment_workflow.impact_review_agent
        original_critic = amendment_workflow.critic_agent_review
        original_drafting = amendment_workflow.amendment_drafting_agent
        try:
            amendment_workflow.impact_review_agent = lambda context: {
                "impact_decision": "Affected",
                "confidence": "Medium",
                "affected_sections": [
                    {
                        "section_id": "P1",
                        "section_title": "Procedure",
                        "original_text": context["selected_section"]["original_text"],
                        "evidence_type": "direct",
                        "rationale": "Direct reference evidence.",
                        "evidence": ["ISO evidence"],
                    }
                ],
                "limitations": [],
            }
            amendment_workflow.critic_agent_review = lambda context, impact: {
                "passed": True,
                "issues_found": [],
                "downgraded_decision": False,
                "final_decision": "Affected",
                "reason": "Evidence is sufficient for draft review.",
            }
            amendment_workflow.amendment_drafting_agent = lambda context, impact, critic: {
                "affected_sections": [
                    {
                        "section_id": "P1",
                        "section_title": "Procedure",
                        "original_text": context["selected_section"]["original_text"],
                        "proposed_text": "Revise procedure to require QA/RA confirmation of updated standard applicability.",
                        "change_type": "revise",
                        "rationale": "Update may affect this controlled activity.",
                        "evidence": ["ISO evidence"],
                        "limitations": [],
                    }
                ],
                "overall_rationale": "Draft only.",
                "suggested_cr_ecr_action": "Open CR/ECR for QA/RA review.",
            }
            with tempfile.TemporaryDirectory() as tmpdir:
                source = Path(tmpdir) / "WI-01-A Test.docx"
                doc = Document()
                doc.add_paragraph("Original controlled section text.")
                doc.save(source)
                original_bytes = source.read_bytes()
                document = sample_document("WI-01-A Test.docx", "P1", "Original controlled section text.")
                document["file_path"] = str(source)
                document["source_file_path"] = str(source)
                row = sample_affected_row(source)

                draft = amendment_workflow.generate_agentic_amendment(row, document, Path(tmpdir) / "drafts", regenerate=True)

                self.assertEqual(source.read_bytes(), original_bytes)
                self.assertTrue(Path(draft["draft_file_path"]).exists())
                self.assertTrue(Path(draft["report_file_path"]).exists())
                self.assertTrue(Path(draft["matrix_file_path"]).exists())
                self.assertNotEqual(Path(draft["draft_file_path"]).resolve(), source.resolve())
                self.assertEqual(draft["qa_ra_review_status"], "Draft only - not approved")
                self.assertTrue(draft["needs_human_confirmation"])
        finally:
            amendment_workflow.impact_review_agent = original_impact
            amendment_workflow.critic_agent_review = original_critic
            amendment_workflow.amendment_drafting_agent = original_drafting

    def test_agentic_amendment_spreadsheet_generates_report_only(self) -> None:
        original_impact = amendment_workflow.impact_review_agent
        original_critic = amendment_workflow.critic_agent_review
        original_drafting = amendment_workflow.amendment_drafting_agent
        try:
            amendment_workflow.impact_review_agent = lambda context: {
                "impact_decision": "Need confirmation",
                "confidence": "Low",
                "affected_sections": [],
                "limitations": [],
            }
            amendment_workflow.critic_agent_review = lambda context, impact: {
                "passed": False,
                "issues_found": ["Spreadsheet edit requires confirmation."],
                "downgraded_decision": True,
                "final_decision": "Need confirmation",
                "reason": "Report only.",
            }
            amendment_workflow.amendment_drafting_agent = lambda context, impact, critic: {
                "affected_sections": [
                    {
                        "section_id": "P1",
                        "section_title": "Spreadsheet row",
                        "original_text": "Spreadsheet requirement",
                        "proposed_text": "QA/RA confirmation required.",
                        "change_type": "need_confirmation",
                        "rationale": "Spreadsheet edit path is report-only.",
                        "evidence": [],
                        "limitations": [],
                    }
                ],
                "overall_rationale": "Report only.",
                "suggested_cr_ecr_action": "Review spreadsheet manually.",
            }
            with tempfile.TemporaryDirectory() as tmpdir:
                source = Path(tmpdir) / "FM001.xlsx"
                pd.DataFrame([{"A": "Spreadsheet requirement"}]).to_excel(source, index=False)
                document = sample_document("FM001.xlsx", "P1", "Spreadsheet requirement", code="FM001")
                document["file_path"] = str(source)
                document["source_file_path"] = str(source)
                row = sample_affected_row(source, code="FM001")

                draft = amendment_workflow.generate_agentic_amendment(row, document, Path(tmpdir) / "drafts", regenerate=True)

                self.assertEqual(draft["draft_file_path"], "")
                self.assertTrue(Path(draft["report_file_path"]).exists())
                self.assertTrue(Path(draft["matrix_file_path"]).exists())
                self.assertIn("Spreadsheet source detected", "\n".join(draft["editor_warnings"]))
        finally:
            amendment_workflow.impact_review_agent = original_impact
            amendment_workflow.critic_agent_review = original_critic
            amendment_workflow.amendment_drafting_agent = original_drafting

    def test_apply_amendment_replaces_matching_docx_paragraph_and_creates_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "WI-01-A Test.docx"
            doc = Document()
            doc.add_paragraph("Original controlled section text.")
            doc.save(source)
            draft = {
                "draft_id": "draft-1",
                "source_file_path": str(source),
                "affected_sections": [
                    {
                        "section_id": "P1",
                        "original_text": "Original controlled section text.",
                        "proposed_text": "Revised controlled section text.",
                    }
                ],
            }

            result = apply_amendment_to_source(draft, Path(tmpdir) / "audit", row_id="row-1")
            updated = Document(source)
            backup_exists = Path(result["backup_file_path"]).exists()

        self.assertEqual(result["status"], "applied")
        self.assertTrue(backup_exists)
        self.assertEqual(updated.paragraphs[0].text, "Revised controlled section text.")

    def test_apply_amendment_fails_when_source_text_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "WI-01-A Test.docx"
            doc = Document()
            doc.add_paragraph("Current controlled section text.")
            doc.save(source)
            original_bytes = source.read_bytes()
            draft = {
                "draft_id": "draft-1",
                "source_file_path": str(source),
                "affected_sections": [
                    {
                        "section_id": "P1",
                        "original_text": "Original controlled section text.",
                        "proposed_text": "Revised controlled section text.",
                    }
                ],
            }

            result = apply_amendment_to_source(draft, Path(tmpdir) / "audit", row_id="row-1")
            unchanged = source.read_bytes() == original_bytes

        self.assertEqual(result["status"], "failed")
        self.assertTrue(unchanged)
        self.assertIn("does not match", result["error"])


def sample_document(file_name: str, section_id: str, text: str, code: str = "WI-01-A", references: dict | None = None) -> dict:
    references = references or {}
    return {
        "file_name": file_name,
        "file_path": f"/tmp/{file_name}",
        "document_code": code,
        "document_title": "WI-01-A Test",
        "document_type": "QSP" if code.startswith("QSP") else "WI",
        "main_sections": [],
        "reference_regulations": references.get("reference_regulations", []),
        "reference_standards": references.get("reference_standards", []),
        "reference_guidance": references.get("reference_guidance", []),
        "reference_documents": references.get("reference_documents", []),
        "reference_forms": references.get("reference_forms", []),
        "document_relationships": references.get("reference_documents", []) + references.get("reference_forms", []),
        "sections": [
            {
                "section_id": section_id,
                "page_or_location": "paragraph",
                "text": text,
                "contains_regulation_reference": False,
            }
        ],
    }


def sample_affected_row(source: Path, code: str = "WI-01-A") -> dict:
    return {
        "regulation_old": "ISO 14971:2019",
        "regulation_latest": "ISO 14971:2025",
        "latest_year": "2025",
        "status": "已更新",
        "change_summary": "Risk-management update summary.",
        "reference_source": "Excel",
        "affected_file": source.name,
        "affected_file_path": str(source),
        "document_code": code,
        "document_title": "Test document",
        "document_type": "WI",
        "candidate_level": "Level 2 - Direct standard/guidance reference",
        "candidate_reason": "Section 3 references the updated standard.",
        "relationship_path": "",
        "reference_evidence": "ISO 14971",
        "amendment_source_mode": "excel_amendment_summary",
        "amendment_confidence": "Medium",
        "exact_clause_verified": False,
        "needs_human_confirmation": True,
        "amendment_summary_used": "Risk-management update summary.",
        "amendment_limitations": "[]",
        "affected_qms_topics": "[]",
        "search_keywords": "[]",
        "ai_decision": "Affected",
        "risk_level": "Medium",
        "reason": "Affected by direct reference.",
        "supporting_evidence": "ISO 14971",
        "recommended_amendment_locations": json.dumps([{"section_id": "P1", "location": "paragraph"}]),
        "section_id": "P1",
        "section_location": "paragraph",
    }


def fake_analyzer(_: dict) -> dict:
    return {"content_summary": "Risk control activity summary", "key_sections": [{"section_id": "P1"}]}


def fake_affected_reviewer(input_data: dict) -> dict:
    return {
        "ai_decision": "Affected",
        "risk_level": "Medium",
        "reason": f"{input_data.get('document_code', '')} is affected.",
        "recommended_amendment_locations": [{"section_id": "P1", "location": "paragraph"}],
        "supporting_evidence": "AI document-level review",
    }


def sample_candidate_row() -> dict:
    row = sample_affected_row(Path("/tmp/WI-01-A Test.docx"))
    row.update(
        {
            "row_id": "row-1",
            "reg_index": 0,
            "review_required": True,
        }
    )
    return row


def section(section_id: str, text: str) -> dict:
    return {
        "section_id": section_id,
        "page_or_location": "paragraph",
        "text": text,
        "contains_regulation_reference": False,
    }


if __name__ == "__main__":
    unittest.main()
