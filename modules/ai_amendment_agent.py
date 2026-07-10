from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


DEFAULT_BASE_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash"
APP_DIR = Path(__file__).resolve().parents[1]


def load_prompt() -> str:
    prompt_path = APP_DIR / "prompts" / "amendment_prompt.md"
    return prompt_path.read_text(encoding="utf-8")


def load_app_env() -> None:
    for env_path in [APP_DIR / ".env", APP_DIR / ".env.txt"]:
        if env_path.exists():
            load_dotenv(env_path)


def completion_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return normalized + "/chat/completions"


def render_prompt(input_data: dict[str, Any]) -> str:
    prompt = load_prompt()
    for name, value in input_data.items():
        prompt = prompt.replace("{" + name + "}", str(value or ""))
    return prompt


def ai_is_configured(api_key: str | None = None) -> bool:
    load_app_env()
    return bool(api_key or os.getenv("AI_API_KEY"))


def generate_amendment(input_data: dict[str, Any], api_key: str | None = None) -> dict[str, Any]:
    load_app_env()
    key = api_key or os.getenv("AI_API_KEY")
    if not key:
        raise ValueError("AI_API_KEY is not configured.")

    base_url = completion_url(os.getenv("AI_BASE_URL", DEFAULT_BASE_URL))
    model = os.getenv("AI_MODEL", DEFAULT_MODEL)
    prompt = render_prompt(input_data)
    response = requests.post(
        base_url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
        },
        timeout=60,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = content.strip("`")
        content = content.removeprefix("json").strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        return {
            "impact_decision": "Need confirmation",
            "risk_level": "Medium",
            "reason": "AI response was not valid JSON.",
            "proposed_amendment_locations": [],
            "suggested_revised_english": "",
            "suggested_revised_chinese": "",
            "reviewer_action": "Regulatory confirmation required",
            "notes": f"JSON parse failure: {exc}; raw response: {content[:1000]}",
        }
