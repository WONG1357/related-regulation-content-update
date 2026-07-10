from __future__ import annotations

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from regulatory_update_app.modules.regulation_suggestions import (
    generate_document_suggestion,
    rank_document_sections,
    validate_ai_suggestion,
)


class RegulationSuggestionTests(unittest.TestCase):
    def test_ranking_excludes_section_3_and_selects_content_section(self) -> None:
        document = sample_document()
        changes = [sample_change()]

        ranked = rank_document_sections(document, changes)

        self.assertEqual([item.section_id for item in ranked], ["P5"])
        self.assertNotIn("P3", [item.section_id for item in ranked])

    def test_no_content_match_returns_without_ai_call(self) -> None:
        calls = []

        def ai_call(*args, **kwargs):
            calls.append((args, kwargs))
            return {}

        result = generate_document_suggestion(
            sample_candidate(),
            sample_document(content="Unrelated warehouse temperature instruction."),
            [sample_change()],
            ai_call=ai_call,
        )

        self.assertEqual(result["decision"], "No content-based amendment suggested")
        self.assertEqual(calls, [])

    def test_prompt_contains_actual_clause_and_section_text(self) -> None:
        seen = {}

        def ai_call(prompt, fallback, timeout):
            seen["prompt"] = prompt
            section_id = "P5"
            evidence_id = next(iter(rank_document_sections(sample_document(), [sample_change()])[0].evidence_ids))
            return {
                "decision": "Affected",
                "confidence": "Medium",
                "reason": "Complaint handling content is affected.",
                "suggestions": [
                    {
                        "section_id": section_id,
                        "proposed_text": "The complaint procedure shall document regulatory reporting evaluation.",
                        "change_type": "revise",
                        "rationale": "New reporting emphasis.",
                        "evidence_ids": [evidence_id],
                    }
                ],
                "limitations": [],
            }

        result = generate_document_suggestion(sample_candidate(), sample_document(), [sample_change()], ai_call=ai_call)

        self.assertIn("Complaint handling shall include regulatory reporting evaluation", seen["prompt"])
        self.assertIn("Current complaint handling and regulatory reporting procedure", seen["prompt"])
        self.assertEqual(result["decision"], "Affected")
        self.assertEqual(result["suggestions"][0]["section_id"], "P5")

    def test_invalid_ai_section_or_evidence_is_suppressed(self) -> None:
        sections = rank_document_sections(sample_document(), [sample_change()])
        raw = {
            "decision": "Affected",
            "confidence": "High",
            "reason": "Invented output.",
            "suggestions": [
                {
                    "section_id": "P999",
                    "proposed_text": "Invented",
                    "evidence_ids": ["missing"],
                }
            ],
            "limitations": [],
        }

        result = validate_ai_suggestion(raw, sections, [sample_change()])

        self.assertEqual(result["decision"], "Need confirmation")
        self.assertEqual(result["suggestions"], [])


def sample_change() -> dict:
    return {
        "evidence_id": "change-1",
        "old_clause_id": "8.2.1",
        "latest_clause_id": "8.2.2",
        "old_clause_title": "Feedback",
        "latest_clause_title": "Complaint handling",
        "old_clause_text": "The organization shall maintain a feedback system.",
        "latest_clause_text": "Complaint handling shall include regulatory reporting evaluation.",
        "added_requirement_units": ["Complaint handling shall include regulatory reporting evaluation."],
        "changed_requirement_units": [],
        "removed_requirement_units": [],
    }


def sample_candidate() -> dict:
    return {
        "document_code": "QSP0809",
        "file_path": "/tmp/QSP0809.docx",
        "depth": 0,
        "seed_code": "QSP0809",
        "relationship_path": "QSP0809",
        "section3_evidence": ["ISO 13485"],
    }


def sample_document(content: str = "Current complaint handling and regulatory reporting procedure shall be maintained.") -> dict:
    return {
        "document_code": "QSP0809",
        "document_title": "Post-market surveillance",
        "file_path": "/tmp/QSP0809.docx",
        "reference_section_start_id": "P2",
        "reference_section_end_id": "P3",
        "sections": [
            {"section_id": "P1", "page_or_location": "paragraph", "text": "1.0 Purpose of the procedure"},
            {"section_id": "P2", "page_or_location": "paragraph", "text": "3.0 References"},
            {"section_id": "P3", "page_or_location": "paragraph", "text": "ISO 13485 Complaint handling"},
            {"section_id": "P5", "page_or_location": "paragraph", "text": content},
        ],
    }


if __name__ == "__main__":
    unittest.main()
