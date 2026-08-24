from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.suggestion_runner import (
    create_suggestion_job,
    load_suggestion_job,
    load_suggestion_results,
    run_suggestion_job,
)


class SuggestionRunnerTests(unittest.TestCase):
    def test_job_persists_partial_results_and_resumes(self) -> None:
        analysis = {
            "resolved_pair": {"family_key": "ISO 13485", "old_year": 2003, "latest_year": 2016},
            "comparison": {"accepted_changes": [sample_change()]},
        }
        nodes = [sample_node("QSP0809", "/tmp/QSP0809.docx"), sample_node("QSP0810", "/tmp/QSP0810.docx")]
        documents = [sample_document("QSP0809", "/tmp/QSP0809.docx"), sample_document("QSP0810", "/tmp/QSP0810.docx")]

        def ai_call(prompt, fallback, timeout):
            return {
                "decision": "No content-based amendment suggested",
                "confidence": "Medium",
                "reason": "No revision required.",
                "suggestions": [],
                "limitations": [],
            }

        with tempfile.TemporaryDirectory() as tmpdir:
            job_id = create_suggestion_job(analysis, nodes, tmpdir)
            run_suggestion_job(job_id, documents, ai_call=ai_call, job_dir=tmpdir, max_candidates=1)
            partial = load_suggestion_job(job_id, tmpdir)
            self.assertEqual(partial["status"], "stopped")
            self.assertEqual(partial["completed_count"], 1)

            run_suggestion_job(job_id, documents, ai_call=ai_call, job_dir=tmpdir)
            completed = load_suggestion_job(job_id, tmpdir)
            results = load_suggestion_results(job_id, tmpdir)

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["completed_count"], 2)
        self.assertEqual(len(results), 2)


def sample_change() -> dict:
    return {
        "evidence_id": "change-1",
        "old_clause_id": "8.2.1",
        "latest_clause_id": "8.2.2",
        "latest_clause_title": "Complaint handling",
        "latest_clause_text": "Complaint handling shall include regulatory reporting evaluation.",
        "added_requirement_units": ["Complaint handling shall include regulatory reporting evaluation."],
    }


def sample_node(code: str, path: str) -> dict:
    return {
        "document_code": code,
        "file_path": path,
        "depth": 0,
        "seed_code": code,
        "relationship_path": code,
        "section3_evidence": ["ISO 13485"],
    }


def sample_document(code: str, path: str) -> dict:
    return {
        "document_code": code,
        "document_title": "Complaint handling",
        "file_path": path,
        "sections": [
            {
                "section_id": "P1",
                "page_or_location": "paragraph",
                "text": "Complaint handling and regulatory reporting evaluation procedure shall be maintained.",
            }
        ],
    }


if __name__ == "__main__":
    unittest.main()
