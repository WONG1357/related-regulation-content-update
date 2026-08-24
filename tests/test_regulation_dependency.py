from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.regulation_dependency import discover_affected_files


class RegulationDependencyTests(unittest.TestCase):
    def test_direct_seed_requires_section3_external_identifier_match(self) -> None:
        direct = document("QSP0001", external=["BS EN ISO 14971:2019"])
        unrelated = document("QSP0002", external=["ISO 13485:2016"])

        result = discover_affected_files([unrelated, direct], ["ISO 14971"])

        self.assertEqual([node.document_code for node in result.nodes], ["QSP0001"])
        node = result.nodes[0]
        self.assertEqual(node.depth, 0)
        self.assertTrue(node.direct_match)
        self.assertEqual(node.seed_code, "QSP0001")
        self.assertEqual(node.relationship_path, ["QSP0001"])
        self.assertEqual(node.section3_evidence[0]["reference_value"], "BS EN ISO 14971:2019")
        json.dumps(result.to_dict())

    def test_forward_traversal_continues_for_more_than_three_hops(self) -> None:
        documents = [
            document("QSP0001", external=["ISO 14971"], internal=["QSP0002"]),
            document("QSP0002", internal=["QSP0003"]),
            document("QSP0003", internal=["QSP0004"]),
            document("QSP0004", internal=["QSP0005"]),
            document("QSP0005"),
        ]

        result = discover_affected_files(documents, ["ISO14971"])
        by_code = {node.document_code: node for node in result.nodes}

        self.assertEqual([by_code[f"QSP000{index}"].depth for index in range(1, 6)], [0, 1, 2, 3, 4])
        self.assertEqual(
            by_code["QSP0005"].relationship_path,
            ["QSP0001", "QSP0002", "QSP0003", "QSP0004", "QSP0005"],
        )
        self.assertEqual(len(by_code["QSP0005"].section3_evidence), 5)

    def test_cycle_is_traversed_once_per_file(self) -> None:
        documents = [
            document("QSP0001", external=["ISO 14971"], internal=["QSP0002"]),
            document("QSP0002", internal=["QSP0003"]),
            document("QSP0003", internal=["QSP0001"]),
        ]

        result = discover_affected_files(documents, ["ISO 14971"])

        self.assertEqual([node.document_code for node in result.nodes], ["QSP0001", "QSP0002", "QSP0003"])
        self.assertEqual([node.depth for node in result.nodes], [0, 1, 2])

    def test_duplicate_paths_choose_deterministic_shortest_path(self) -> None:
        documents = [
            document("QSP0001", external=["ISO 14971"], internal=["QSP0003", "QSP0002"]),
            document("QSP0002", internal=["QSP0004"]),
            document("QSP0003", internal=["QSP0004"]),
            document("QSP0004"),
        ]

        result = discover_affected_files(documents, ["ISO 14971"])
        target = next(node for node in result.nodes if node.document_code == "QSP0004")

        self.assertEqual(target.depth, 2)
        self.assertEqual(target.relationship_path, ["QSP0001", "QSP0002", "QSP0004"])

    def test_minimum_depth_wins_across_multiple_direct_seeds(self) -> None:
        documents = [
            document("QSP0001", external=["ISO 14971"], internal=["QSP0002"]),
            document("QSP0002", internal=["QSP0004"]),
            document("QSP0003", external=["ISO 14971"], internal=["QSP0004"]),
            document("QSP0004"),
        ]

        result = discover_affected_files(documents, ["ISO 14971"])
        target = next(node for node in result.nodes if node.document_code == "QSP0004")

        self.assertEqual(target.depth, 1)
        self.assertEqual(target.seed_code, "QSP0003")
        self.assertEqual(target.relationship_path, ["QSP0003", "QSP0004"])

    def test_unresolved_codes_are_audit_only_and_keep_raw_evidence(self) -> None:
        seed = document("QSP0001", external=["ISO 14971"])
        seed["reference_items"] = [
            {
                "reference_code": "QSP9999",
                "reference_text": "QSP9999 Missing controlled procedure",
                "match_status": "unresolved",
                "matched_files": [],
            }
        ]
        seed["unresolved_internal_references"] = ["QSP9999"]

        result = discover_affected_files([seed], ["ISO 14971"])

        self.assertEqual([node.document_code for node in result.nodes], ["QSP0001"])
        self.assertEqual(result.unresolved_codes, ["QSP9999"])
        self.assertEqual(result.unresolved_references[0]["source_document_code"], "QSP0001")
        self.assertEqual(
            result.unresolved_references[0]["raw_reference_text"],
            "QSP9999 Missing controlled procedure",
        )

    def test_attach_reference_match_fields_are_supported(self) -> None:
        seed = document("QSP0001", external=["ISO 14971"])
        seed["reference_items"] = [
            {
                "reference_code": "QSP0002",
                "reference_text": "QSP0002 Referenced procedure",
                "match_status": "matched",
                "matched_files": [
                    {
                        "document_code": "QSP0002",
                        "file_name": "QSP0002-A.docx",
                        "file_path": "/tmp/QSP0002-A.docx",
                    }
                ],
            }
        ]
        seed["matched_reference_files"] = seed["reference_items"][0]["matched_files"]

        result = discover_affected_files([document("QSP0002"), seed], ["ISO 14971"])

        self.assertEqual([node.document_code for node in result.nodes], ["QSP0001", "QSP0002"])
        self.assertEqual(result.nodes[1].section3_evidence[-1]["reference_value"], "QSP0002 Referenced procedure")

    def test_reference_audit_index_string_fields_are_supported(self) -> None:
        seed = document("QSP0001", external=["ISO 14971"])
        seed["document_relationships"] = []
        seed["reference_documents"] = []
        seed["internal_references"] = "QSP0003, QSP0002"

        result = discover_affected_files(
            [seed, document("QSP0002"), document("QSP0003")],
            ["ISO 14971"],
        )

        self.assertEqual([node.document_code for node in result.nodes], ["QSP0001", "QSP0002", "QSP0003"])

    def test_result_order_does_not_depend_on_input_or_reference_order(self) -> None:
        first = document("QSP0001", external=["ISO14971"], internal=["QSP0003", "QSP0002"])
        documents = [first, document("QSP0002"), document("QSP0003")]
        reversed_first = document("QSP0001", external=["ISO14971"], internal=["QSP0002", "QSP0003"])
        reversed_documents = [document("QSP0003"), document("QSP0002"), reversed_first]

        normal = discover_affected_files(documents, ["ISO14971", "ISO 14971"]).to_dict()
        reversed_result = discover_affected_files(reversed_documents, ["ISO 14971", "ISO14971"]).to_dict()

        self.assertEqual(normal, reversed_result)

    def test_identifier_boundaries_prevent_generic_substring_and_filename_matches(self) -> None:
        documents = [
            document("QSP0001", external=["ISO 149710:2019"]),
            document("QSP0002", external=["MYISO 14971"]),
            document(
                "QSP0003",
                file_name="QSP0003 ISO 14971 procedure.docx",
                body_text="ISO 14971 appears in content.",
            ),
            document("QSP0004", external=["BS EN ISO 14971:2019"]),
            document("QSP0005", external=["ISO14971A"]),
        ]

        result = discover_affected_files(documents, ["ISO 14971"])

        self.assertEqual([node.document_code for node in result.nodes], ["QSP0004"])

    def test_explicit_missing_section3_fails_closed(self) -> None:
        stale = document("QSP0001", external=["ISO 14971"])
        stale["section3_found"] = False

        result = discover_affected_files([stale], ["ISO 14971"])

        self.assertEqual(result.nodes, [])


def document(
    code: str,
    *,
    external: list[str] | None = None,
    internal: list[str] | None = None,
    file_name: str | None = None,
    body_text: str = "Procedure body",
) -> dict:
    external = list(external or [])
    internal = list(internal or [])
    name = file_name or f"{code}-A.docx"
    return {
        "document_code": code,
        "document_title": f"{code} Test document",
        "document_type": "QSP",
        "file_name": name,
        "file_path": f"/tmp/{name}",
        "reference_regulations": [],
        "reference_standards": external,
        "reference_guidance": [],
        "external_references": external,
        "reference_documents": internal,
        "reference_forms": [],
        "document_relationships": internal,
        "sections": [{"section_id": "P1", "text": body_text}],
    }


if __name__ == "__main__":
    unittest.main()
