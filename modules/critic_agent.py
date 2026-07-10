from __future__ import annotations

import json
from typing import Any

from .ai_document_agent import ai_json
from .reference_graph import GENERIC_TOPIC_WORDS


def critic_agent_review(context: dict[str, Any], impact_review: dict[str, Any]) -> dict[str, Any]:
    prompt = f"""
You are a strict QA/RA critic agent.

Check whether the impact review is evidence-based and conservative.

Generic weak topic words:
{", ".join(sorted(GENERIC_TOPIC_WORDS))}

Context:
{json.dumps(context, ensure_ascii=False, indent=2)}

Impact review:
{json.dumps(impact_review, ensure_ascii=False, indent=2)}

Rules:
- Downgrade to Need confirmation when evidence is only broad topic words.
- Downgrade to Need confirmation when the selected section is not clearly relevant.
- Prevent hallucinated exact clause changes.
- If source mode is excel_amendment_summary, require a limitation statement.
- If exact_clause_verified is false, require a limitation statement.
- Output valid JSON only.

Output JSON:
{{
  "passed": true,
  "issues_found": [],
  "downgraded_decision": false,
  "final_decision": "Affected | Need confirmation | Not affected",
  "reason": ""
}}
""".strip()
    result = ai_json(
        prompt,
        {
            "passed": False,
            "issues_found": ["Critic response was not valid JSON."],
            "downgraded_decision": True,
            "final_decision": "Need confirmation",
            "reason": "QA/RA confirmation is required because critic review could not be parsed.",
        },
        timeout=120,
    )
    if context.get("amendment_profile", {}).get("exact_clause_verified") is False:
        result.setdefault("issues_found", [])
        if "Exact clause-level amendment is not verified." not in result["issues_found"]:
            result["issues_found"].append("Exact clause-level amendment is not verified.")
    return result
