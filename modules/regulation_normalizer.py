from __future__ import annotations

import re


STRONG_ALIASES = {
    "ISO 13485": ["ISO 13485", "ISO13485", "EN ISO 13485", "BS EN ISO 13485", "YY/T 0287", "YY/T0287"],
    "ISO 14971": ["ISO 14971", "ISO14971", "EN ISO 14971", "BS EN ISO 14971", "YY/T 0316", "YY/T0316"],
    "EU MDR 2017/745": ["Regulation (EU) 2017/745", "Regulation EU 2017/745", "EU MDR", "2017/745"],
    "EU MDD 93/42/EEC": ["MDD 93/42/EEC", "93/42/EEC", "Medical Device Directive"],
}

WEAK_ALIASES = {
    "ISO 13485": ["质量管理体系"],
    "ISO 14971": ["风险管理"],
}


def normalize_standard_name(raw_text: object) -> str:
    text = str(raw_text or "").strip()
    compact = re.sub(r"\s+", " ", text).upper()
    compact = compact.replace("：", ":")

    if re.search(r"2017/745|EU\s*MDR|REGULATION\s*\(?EU\)?\s*2017/745", compact):
        return "EU MDR 2017/745"
    if re.search(r"93/42/EEC|MDD", compact):
        return "EU MDD 93/42/EEC"

    yy_match = re.search(r"YY/T\s*0?(\d+)", compact)
    if yy_match:
        code = yy_match.group(1)
        if code == "287":
            return "ISO 13485"
        if code == "316":
            return "ISO 14971"

    iso_match = re.search(r"ISO\s*(\d+(?:-\d+)?)", compact)
    if iso_match:
        return f"ISO {iso_match.group(1)}"

    iec_match = re.search(r"IEC\s*(\d+(?:-\d+)?)", compact)
    if iec_match:
        return f"IEC {iec_match.group(1)}"

    astm_match = re.search(r"ASTM\s*([A-Z]\s*\d+[A-Z0-9/ -]*)", compact)
    if astm_match:
        return "ASTM " + re.sub(r"\s+", "", astm_match.group(1)).split(":")[0]

    return text


def aliases_for(old_standard: object, latest_standard: object | None = None) -> list[dict[str, str]]:
    names = {normalize_standard_name(old_standard)}
    if latest_standard:
        names.add(normalize_standard_name(latest_standard))

    aliases: list[dict[str, str]] = []
    for name in names:
        aliases.append({"alias": name, "strength": "strong"})
        for alias in STRONG_ALIASES.get(name, []):
            aliases.append({"alias": alias, "strength": "strong"})
        for alias in WEAK_ALIASES.get(name, []):
            aliases.append({"alias": alias, "strength": "weak"})

    raw_old = str(old_standard or "").strip()
    if raw_old:
        aliases.append({"alias": raw_old, "strength": "strong"})
    return _dedupe_aliases(aliases)


def alias_regex(alias: str) -> re.Pattern[str]:
    escaped = re.escape(alias).replace(r"\ ", r"\s*")
    return re.compile(escaped, re.IGNORECASE)


def _dedupe_aliases(aliases: list[dict[str, str]]) -> list[dict[str, str]]:
    seen = set()
    result = []
    for item in aliases:
        key = (item["alias"].lower(), item["strength"])
        if item["alias"] and key not in seen:
            seen.add(key)
            result.append(item)
    return result
