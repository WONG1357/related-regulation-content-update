from __future__ import annotations

from io import BytesIO
import json

import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


def dataframe_to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


def dataframe_to_excel_bytes(df: pd.DataFrame, sheet_name: str = "affected_documents") -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name[:31])
    return output.getvalue()


def audit_workbook_bytes(sheets: dict[str, pd.DataFrame]) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for raw_name, frame in sheets.items():
            name = unique_sheet_name(raw_name, writer.sheets)
            export_frame = frame.copy()
            for column in export_frame.columns:
                export_frame[column] = export_frame[column].map(excel_safe_value)
            export_frame.to_excel(writer, index=False, sheet_name=name)
            worksheet = writer.book[name]
            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = worksheet.dimensions
            worksheet.sheet_view.showGridLines = False
            for cell in worksheet[1]:
                cell.fill = PatternFill("solid", fgColor="0F766E")
                cell.font = Font(color="FFFFFF", bold=True)
                cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
            for index, column in enumerate(worksheet.iter_cols(), start=1):
                max_length = max((len(str(cell.value or "")) for cell in column), default=0)
                worksheet.column_dimensions[get_column_letter(index)].width = min(max(max_length + 2, 12), 55)
                for cell in column[1:]:
                    cell.alignment = Alignment(vertical="top", wrap_text=True)
            worksheet.row_dimensions[1].height = 28
    return output.getvalue()


def excel_safe_value(value):
    if isinstance(value, (list, dict, tuple, set)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


def unique_sheet_name(raw_name: str, existing: dict[str, object]) -> str:
    base = str(raw_name or "Sheet")[:31]
    candidate = base
    suffix = 1
    while candidate in existing:
        tail = f"_{suffix}"
        candidate = base[: 31 - len(tail)] + tail
        suffix += 1
    return candidate
