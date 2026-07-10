from __future__ import annotations

import json
from typing import Any

from .ai_document_agent import ai_json


PROMPT_VERSION = "agentic-amendment-v1"


def impact_review_agent(context: dict[str, Any]) -> dict[str, Any]:
    prompt = f"""
You are a cautious medical-device QA/RA impact review agent.

Review one selected regulation/document/section pair. Use only the supplied evidence.
Do not invent exact clause changes.

Regulation update:
{json.dumps(context.get("regulation_update", {}), ensure_ascii=False, indent=2)}

Amendment profile:
{json.dumps(context.get("amendment_profile", {}), ensure_ascii=False, indent=2)}

Candidate evidence:
{json.dumps(context.get("candidate_evidence", {}), ensure_ascii=False, indent=2)}

Controlled document:
{json.dumps(context.get("document", {}), ensure_ascii=False, indent=2)}

Selected section:
{json.dumps(context.get("selected_section", {}), ensure_ascii=False, indent=2)}

Instructions:
- Decide only Affected, Need confirmation, or Not affected.
- Identify affected section(s) and cite the original section text.
- Classify evidence as direct, indirect, topic-based, or weak.
- If source mode is excel_amendment_summary and exact_clause_verified is false, state that clause-level change is not verified.
- Output valid JSON only.

Output JSON:
{{
  "impact_decision": "Affected | Need confirmation | Not affected",
  "confidence": "High | Medium | Low",
  "affected_sections": [
    {{
      "section_id": "",
      "section_title": "",
      "original_text": "",
      "evidence_type": "direct | indirect | topic-based | weak",
      "rationale": "",
      "evidence": []
    }}
  ],
  "limitations": []
}}
""".strip()
    return ai_json(
        prompt,
        {
            "impact_decision": "Need confirmation",
            "confidence": "Low",
            "affected_sections": [
                {
                    "section_id": context.get("selected_section", {}).get("section_id", ""),
                    "section_title": context.get("selected_section", {}).get("section_title", ""),
                    "original_text": context.get("selected_section", {}).get("original_text", ""),
                    "evidence_type": "weak",
                    "rationale": "AI response was not valid JSON; QA/RA confirmation is required.",
                    "evidence": [],
                }
            ],
            "limitations": ["AI impact review could not be parsed."],
        },
        timeout=120,
    )


def amendment_drafting_agent(context: dict[str, Any], impact_review: dict[str, Any], critic_review: dict[str, Any]) -> dict[str, Any]:
    prompt = f"""
You are a controlled-document amendment drafting agent for a medical-device QMS.

Draft only the affected section. Do not rewrite the whole document. Do not invent regulatory requirements.
Preserve numbering, terminology, and bilingual style where visible.

Context:
{json.dumps(context, ensure_ascii=False, indent=2)}

Impact review:
{json.dumps(impact_review, ensure_ascii=False, indent=2)}

Critic review:
{json.dumps(critic_review, ensure_ascii=False, indent=2)}

Rules:
- If the final decision is Need confirmation, draft cautious review wording or mark change_type as need_confirmation.
- If source mode is excel_amendment_summary, include the limitation that this is not a verified clause-by-clause comparison.
- If exact_clause_verified is false, do not claim exact clause changes.
- Include QA/RA confirmation required.
- Output valid JSON only.

Output JSON:
{{
  "affected_sections": [
    {{
      "section_id": "",
      "section_title": "",
      "original_text": "",
      "proposed_text": "",
      "change_type": "add | revise | delete | no_change | need_confirmation",
      "rationale": "",
      "evidence": [],
      "limitations": []
    }}
  ],
  "overall_rationale": "",
  "suggested_cr_ecr_action": ""
}}
""".strip()
    return ai_json(
        prompt,
        {
            "affected_sections": [
                {
                    "section_id": context.get("selected_section", {}).get("section_id", ""),
                    "section_title": context.get("selected_section", {}).get("section_title", ""),
                    "original_text": context.get("selected_section", {}).get("original_text", ""),
                    "proposed_text": (
                        "[DRAFT AMENDMENT - NOT APPROVED]\n"
                        "QA/RA confirmation is required before revising this controlled-document section."
                    ),
                    "change_type": "need_confirmation",
                    "rationale": "AI drafting response was not valid JSON.",
                    "evidence": [],
                    "limitations": ["Draft wording could not be generated reliably."],
                }
            ],
            "overall_rationale": "Draft generation needs QA/RA confirmation.",
            "suggested_cr_ecr_action": "Open a controlled document-change request for QA/RA review if impact is confirmed.",
        },
        timeout=120,
    )
