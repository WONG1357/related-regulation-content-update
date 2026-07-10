from __future__ import annotations

import json
import os
from typing import Any

import requests

from .ai_amendment_agent import DEFAULT_BASE_URL, DEFAULT_MODEL, completion_url, load_app_env
from .document_cache import document_text


MAX_DOCUMENT_CHARS = 60000


def ai_json(prompt: str, fallback: dict[str, Any], api_key: str | None = None, timeout: int = 120) -> dict[str, Any]:
    load_app_env()
    key = api_key or os.getenv("AI_API_KEY")
    if not key:
        raise ValueError("AI_API_KEY is not configured.")

    response = requests.post(
        completion_url(os.getenv("AI_BASE_URL", DEFAULT_BASE_URL)),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": os.getenv("AI_MODEL", DEFAULT_MODEL),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = content.strip("`")
        content = content.removeprefix("json").strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        result = fallback.copy()
        result["notes"] = f"JSON parse failure: {exc}; raw response: {content[:1000]}"
        return result


def summarize_document(document: dict[str, Any], api_key: str | None = None) -> dict[str, Any]:
    prompt = f"""
You are a medical-device ISO 13485 QMS document-control assistant.

Task:
Deeply analyze the full controlled document content below. Produce a reusable document profile for later regulatory impact review. Do not decide regulatory impact yet.

Document metadata:
- Code: {document.get("document_code", "")}
- Title: {document.get("document_title", "")}
- Type: {document.get("document_type", "")}
- File: {document.get("file_name", "")}

Full extracted document content:
{document_text(document, MAX_DOCUMENT_CHARS)}

Instructions:
- Focus on the actual operating instruction, QMS process, validation, inspection, labeling, regulatory, risk, PMS, complaint, purchasing, production, warehouse, cleanroom, calibration, or test activities controlled by the document.
- Identify sections that may be relevant to regulatory or standard changes, even if no standard number appears.
- Output valid JSON only.

Output JSON format:
{{
  "content_summary": "Concise but specific summary of the document purpose, controlled activities, products/processes, and regulatory/QMS relevance.",
  "key_sections": [
    {{"section_id": "...", "location": "...", "summary": "...", "excerpt": "..."}}
  ]
}}
""".strip()
    return ai_json(prompt, {"content_summary": "", "key_sections": []}, api_key=api_key)


def review_regulation_document_pair(input_data: dict[str, Any], api_key: str | None = None) -> dict[str, Any]:
    prompt = f"""
You are a medical-device regulatory affairs and ISO 13485 QMS document-control assistant.

Task:
Decide whether this updated regulation/standard affects this controlled document. This is a document-level review. Do not rely only on keyword matches; analyze the document purpose, controlled activities, and the regulatory change summary.

Regulation update:
- Old standard/regulation: {input_data.get("old_standard", "")}
- Latest standard/regulation: {input_data.get("latest_standard", "")}
- Latest year: {input_data.get("latest_year", "")}
- Status: {input_data.get("status", "")}
- Main change summary: {input_data.get("change_summary", "")}
- Reference source: {input_data.get("reference_source", "")}

Amendment profile:
- Source mode: {input_data.get("amendment_source_mode", "")}
- Confidence: {input_data.get("amendment_confidence", "")}
- Amendment summary used: {input_data.get("amendment_summary_used", "")}
- Exact clause verified: {input_data.get("exact_clause_verified", "")}
- Needs human confirmation: {input_data.get("needs_human_confirmation", "")}
- Affected QMS topics: {json.dumps(input_data.get("affected_qms_topics", []), ensure_ascii=False)}
- Search keywords: {json.dumps(input_data.get("search_keywords", []), ensure_ascii=False)}
- Limitations: {json.dumps(input_data.get("amendment_limitations", []), ensure_ascii=False)}

Controlled document:
- Code: {input_data.get("document_code", "")}
- Title: {input_data.get("document_title", "")}
- Type: {input_data.get("document_type", "")}
- File: {input_data.get("file_name", "")}
- Content summary: {input_data.get("content_summary", "")}
- Key sections: {json.dumps(input_data.get("key_sections", []), ensure_ascii=False)}
- Main sections: {json.dumps(input_data.get("main_sections", []), ensure_ascii=False)}
- Section 3 references: {json.dumps(input_data.get("section_3_references", {}), ensure_ascii=False)}
- Candidate selection level: {input_data.get("candidate_level", "")}
- Candidate selection reason: {input_data.get("candidate_reason", "")}
- Relationship path: {input_data.get("relationship_path", "")}
- Reference evidence: {input_data.get("reference_evidence", "")}

Supporting alias evidence:
{json.dumps(input_data.get("alias_evidence", []), ensure_ascii=False)}

Instructions:
- Output "Affected" only when the regulation update plausibly requires document review or amendment.
- Output "Not affected" when the document is unrelated or only contains broad weak terms without meaningful process impact.
- Output "Need confirmation" when the available regulation update summary is insufficient but the document may plausibly be in scope.
- Broad Chinese terms such as 风险管理 and 质量管理体系 are weak hints only, not strong evidence.
- If source mode is excel_amendment_summary, excel_reference_hint, or no_amendment_evidence, do not claim exact clause-level changes. Use cautious wording such as "According to the Excel amendment summary..." and state that exact clause-level change could not be verified.
- If exact clause verified is false, recommend document review areas only; do not write that a specific regulation clause changed from one requirement to another.
- All results that rely on Excel fallback or reference hints must mention that human QA/RA confirmation is required.
- Recommend amendment locations at document section/location level. Do not limit locations to keyword-hit sections.
- Output valid JSON only.

Output JSON format:
{{
  "ai_decision": "Affected / Not affected / Need confirmation",
  "risk_level": "High / Medium / Low",
  "reason": "...",
  "recommended_amendment_locations": [
    {{"section_id": "...", "location": "...", "reason": "..."}}
  ],
  "supporting_evidence": "...",
  "notes": "..."
}}
""".strip()
    return ai_json(
        prompt,
        {
            "ai_decision": "Need confirmation",
            "risk_level": "Medium",
            "reason": "AI response was not valid JSON.",
            "recommended_amendment_locations": [],
            "supporting_evidence": "",
            "notes": "",
        },
        api_key=api_key,
    )
