from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd


PREFERRED_SHEET = "原始总表"


@dataclass
class ExcelLoadResult:
    sheets: list[str]
    selected_sheet: str
    dataframe: pd.DataFrame
    updated_regulations: pd.DataFrame
    stats: dict[str, int]
    warnings: list[str]


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    result.columns = [str(col).strip() for col in result.columns]
    return result


def choose_sheet(path: str | Path, requested_sheet: str | None = None) -> tuple[list[str], str]:
    with pd.ExcelFile(path) as excel:
        sheets = excel.sheet_names
    if requested_sheet and requested_sheet in sheets:
        return sheets, requested_sheet
    if PREFERRED_SHEET in sheets:
        return sheets, PREFERRED_SHEET
    return sheets, sheets[0]


def load_regulation_excel(
    path: str | Path,
    recent_year: int = 2024,
    requested_sheet: str | None = None,
) -> ExcelLoadResult:
    sheets, selected_sheet = choose_sheet(path, requested_sheet)
    df = normalize_columns(pd.read_excel(path, sheet_name=selected_sheet))
    warnings = []

    required = ["原标准编号", "最新版本", "最新年份", "状态"]
    missing = [col for col in required if col not in df.columns]
    if missing:
        warnings.append(f"Missing expected columns: {', '.join(missing)}")

    status = df.get("状态", pd.Series([""] * len(df))).astype(str)
    latest_year = pd.to_numeric(df.get("最新年份", pd.Series([None] * len(df))), errors="coerce")
    update_mask = status.str.contains("已更新", na=False)
    updated = df[update_mask].copy()

    reconfirm_mask = status.str.contains("当前版本（再确认）", na=False)
    current_mask = status.str.contains("当前版本", na=False) & ~reconfirm_mask
    stats = {
        "total_rows": int(len(df)),
        "updated_rows": int(status.str.contains("已更新", na=False).sum()),
        "current_rows": int(current_mask.sum()),
        "need_confirmation_rows": int(
            (
                status.str.contains("需要确认", na=False)
                | reconfirm_mask
            ).sum()
        ),
        "selected_update_candidates": int(len(updated)),
    }
    return ExcelLoadResult(sheets, selected_sheet, df, updated, stats, warnings)
