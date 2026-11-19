"""Independent Streamlit entry point for ISO publication discovery and evidence review."""
from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from modules.app_paths import data_root
from modules.standards_catalogue import fetch_iso_catalogue, load_iso_catalogue
from modules.standards_query import QuerySpec, default_query, parse_query
from modules.standards_update_workflow import (
    compare_discovered_pair, report_json, report_markdown, run_update_workflow,
)


st.set_page_config(page_title="Standards Updates", page_icon="📚", layout="wide")


def split_values(value: str) -> list[str]:
    return [item.strip() for item in value.replace("，", ",").replace("\n", ",").split(",") if item.strip()]


def store_upload(upload, category: str) -> Path:
    raw = upload.getvalue()
    if not raw:
        raise ValueError("上傳的檔案是空白的。")
    suffix = Path(upload.name).suffix.lower()
    if suffix not in {".jsonl", ".txt", ".pdf", ".docx"}:
        raise ValueError("不支援此檔案格式。")
    folder = data_root() / "standards" / category
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (hashlib.sha256(raw).hexdigest() + suffix)
    if not path.exists():
        path.write_bytes(raw)
    return path


def catalogue(source_mode, upload=None, refresh=False):
    if source_mode == "ISO 官方目錄":
        cached = st.session_state.get("standards_snapshot")
        if cached is not None and st.session_state.get("standards_source_mode") == source_mode and not refresh:
            return cached
        # Never silently substitute an older snapshot after a failed refresh.
        st.session_state.pop("standards_snapshot", None)
        with st.spinner("下載及驗證 ISO 官方目錄；首次約需讀取 80 MB…"):
            snapshot = fetch_iso_catalogue(data_root() / "standards" / "catalogues")
    else:
        if upload is None:
            raise ValueError("請先上傳 ISO Open Data 格式的 JSONL 目錄。")
        path = store_upload(upload, "catalogues")
        snapshot = load_iso_catalogue(path, source_url="user-supplied ISO-format JSONL (origin not independently verified)")
    st.session_state["standards_snapshot"] = snapshot
    st.session_state["standards_source_mode"] = source_mode
    return snapshot


st.title("標準更新與版本比較")
st.write("指定日期與標準範圍，查找 ISO 發布記錄，再比較你提供的新舊版本。")
st.caption("目前接通 ISO Open Data。IEC、ASTM 及各地法規資料源尚未接通。")

with st.sidebar:
    st.header("資料來源")
    source_mode = st.radio("目錄", ["ISO 官方目錄", "上傳目錄快照"])
    snapshot_upload = st.file_uploader("ISO JSONL", type=["jsonl"], key="catalogue_upload") if source_mode != "ISO 官方目錄" else None
    if st.button("更新／載入目錄", width="stretch"):
        st.session_state.pop("standards_report", None)
        st.session_state.pop("standards_snapshot", None)
        try:
            catalogue(source_mode, snapshot_upload, refresh=True)
            st.success("目錄已載入。")
        except Exception as exc:
            st.error(f"目錄未能載入：{exc}")
    snapshot = st.session_state.get("standards_snapshot")
    if snapshot is not None:
        st.caption(f"已載入來源：{st.session_state.get('standards_source_mode', 'unknown')}")
        st.metric("目錄記錄", f"{len(snapshot.records):,}")
        st.caption(f"取得時間：{snapshot.retrieved_at}")
        st.caption("取得時間不等於標準發布日期；目前目錄也不是完整歷史事件資料庫。")
    st.divider()
    st.markdown("[ISO Open Data](https://www.iso.org/open-data.html)")
    st.caption("Based on ISO iso_deliverables_metadata, ODC-By 1.0. ISO attribution is included in exports.")

search_tab, compare_tab, audit_tab = st.tabs(["1 · 查找更新", "2 · 比較版本", "3 · 證據與匯出"])

with search_tab:
    entry = st.radio("輸入方式", ["日期與範圍", "自然語言"], horizontal=True)
    defaults = default_query()
    with st.form("standards_query_form"):
        if entry == "日期與範圍":
            first, second = st.columns(2)
            start = first.date_input("開始日期（包含當日）", defaults.start_date)
            end = second.date_input("結束日期（包含當日）", defaults.end_date)
            scope_mode = st.selectbox("查詢範圍", ["標準編號清單", "ICS 分類", "全部 ISO"])
            scope = st.text_area("標準編號或 ICS 分類；每行一項", placeholder="ISO 13485\nISO 14971\nISO 10993-*", help="標準編號須保留 part；例如 ISO 10993-1。ISO 10993-* 表示整個系列。ICS 11 包括醫療及保健技術，不只醫療器械。")
            committee = st.text_input("進一步限制 Technical Committee（選填）", placeholder="ISO/TC 210")
            keywords = st.text_input("進一步限制英文關鍵字（選填）", placeholder="medical devices")
        else:
            request = st.text_area("你想查甚麼？", placeholder="查過去一年 ISO 13485、ISO 14971、ISO 10993-* 的更新")
            use_ai = st.checkbox("使用已設定的 AI 模型理解較複雜的查詢", value=False)
            st.caption("AI 僅將查詢文字轉成日期與範圍；目錄篩選由程式執行。版本全文不會送到 AI。未設定 AI 時可直接使用日期表單。")
        submitted = st.form_submit_button("查找更新", type="primary")
    if submitted:
        st.session_state.pop("standards_report", None)
        try:
            if entry == "日期與範圍":
                items = split_values(scope)
                if scope_mode != "全部 ISO" and not items:
                    raise ValueError("請輸入標準編號／ICS 分類，或明確選擇『全部 ISO』。")
                query = QuerySpec.from_dict({
                    "publisher": "ISO", "start_date": start.isoformat(), "end_date": end.isoformat(),
                    "standards": items if scope_mode == "標準編號清單" else [],
                    "ics_prefixes": items if scope_mode == "ICS 分類" else [],
                    "committees": split_values(committee), "keywords": split_values(keywords),
                })
            else:
                query = parse_query(request, use_ai=use_ai)
            snapshot = catalogue(source_mode, snapshot_upload)
            with st.spinner("按發布日期篩選並追查替代關係…"):
                st.session_state["standards_report"] = run_update_workflow(snapshot, query)
        except Exception as exc:
            st.error(f"本次查詢未完成：{exc}")
    report = st.session_state.get("standards_report")
    if report:
        st.subheader(f"找到 {len(report['updates']):,} 筆發布記錄")
        st.write("查詢日期和範圍", report["query"])
        st.info("這是指定目錄快照內的發布記錄。未找到記錄，不代表所有來源都已確認沒有更新。歷史確認、撤回和修正版事件未完整涵蓋。")
        rows = [{
            "標準": row["reference"], "發布日期": row["publication_date"], "事件": row["event_type"],
            "替代版本": "; ".join(str(p.get("reference") or p["id"]) for p in row.get("predecessors", [])),
            "目前 Stage": row.get("current_stage"), "比較狀態": row.get("content_comparison", {}).get("status", "not_run"),
            "來源": row["source_url"],
        } for row in report["updates"]]
        if rows:
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                         column_config={"來源": st.column_config.LinkColumn("來源")})
        for warning in report.get("warnings", []):
            st.warning(str(warning))

with compare_tab:
    report = st.session_state.get("standards_report")
    snapshot = st.session_state.get("standards_snapshot")
    if not report or snapshot is None:
        st.info("先執行一次更新查詢，再選擇要比較的版本。")
    else:
        updates = {row["id"]: row for row in report["updates"]}
        if not updates:
            st.info("本次目錄查詢沒有可供選擇的發布記錄。")
        else:
            selected = st.selectbox("選擇發布記錄", list(updates), format_func=lambda key: updates[key]["reference"])
            update = updates[selected]
            st.write(update["title"])
            st.caption(update["next_action"])
            for warning in update.get("relationship_warnings", []):
                st.warning(str(warning))
            comparable = [pid for pid in update.get("replaces", []) if pid in snapshot.records and not snapshot.records[pid].supplement_type]
            if update.get("supplement_type") or not comparable:
                st.info("此記錄需要補充版本關係或 Amendment／base edition 資料；目前不能當成兩個完整版本直接比較。")
            else:
                old_id = st.selectbox("選擇官方列出的上一版本", comparable, format_func=lambda key: snapshot.records[key].reference)
                left, right = st.columns(2)
                old_upload = left.file_uploader(f"舊版：{snapshot.records[old_id].reference}", type=["txt", "pdf", "docx"], key=f"old_{selected}_{old_id}")
                new_upload = right.file_uploader(f"新版：{update['reference']}", type=["txt", "pdf", "docx"], key=f"new_{selected}_{old_id}")
                st.caption("請核對檔案封面、版本、語言及適用修訂。此處在本機抽取文字及比較；結果是待審核差異，並非完整合規結論。")
                if st.button("產生差異證據", type="primary", disabled=old_upload is None or new_upload is None):
                    try:
                        with st.spinner("比較章節、附錄與文字差異…"):
                            compare_discovered_pair(report, snapshot, update_id=selected, predecessor_id=old_id,
                                                    old_path=store_upload(old_upload, "documents"), new_path=store_upload(new_upload, "documents"))
                        st.rerun()
                    except Exception as exc:
                        st.error(f"本次比較未完成：{exc}")
            results = [item for item in report.get("comparisons", []) if item.get("catalogue_link", {}).get("new_id") == selected]
            for result in results:
                st.subheader(f"{result['catalogue_link']['old_reference']} → {result['catalogue_link']['new_reference']}")
                st.write("狀態", result["status"])
                st.caption(f"已完成的比較：{result.get('compared_at', '')}。更換上傳檔案後，請重新執行比較以產生新結果。")
                with st.expander("此比較使用的檔案指紋"):
                    st.code("Old SHA-256: " + str(result.get("old_document", {}).get("sha256", "")) +
                            "\nNew SHA-256: " + str(result.get("new_document", {}).get("sha256", "")))
                for warning in result.get("warnings", []):
                    st.warning(str(warning))
                for change in result.get("changes", []):
                    with st.expander(f"{change.get('old_clause', '—')} → {change.get('new_clause', '—')} · {change.get('change_type', '')}"):
                        if change.get("critical_tokens_changed"):
                            st.warning("需特別核對：" + ", ".join(change["critical_tokens_changed"]))
                        col_old, col_new = st.columns(2)
                        col_old.caption(str(change.get("old_locator", "")))
                        col_old.text(change.get("old_text", ""))
                        col_new.caption(str(change.get("new_locator", "")))
                        col_new.text(change.get("new_text", ""))

with audit_tab:
    report = st.session_state.get("standards_report")
    if not report:
        st.info("查詢完成後，這裡會顯示來源、覆蓋範圍、執行步驟和完整匯出。")
    else:
        st.subheader("來源與覆蓋範圍")
        st.json({"source": report.get("source"), "coverage": report.get("coverage")})
        st.subheader("執行步驟")
        st.json(report["trace"])
        left, right = st.columns(2)
        left.download_button("下載完整 JSON", report_json(report), f"standards-update-{report['run_id']}.json", mime="application/json")
        right.download_button("下載 Markdown 報告", report_markdown(report), f"standards-update-{report['run_id']}.md", mime="text/markdown")
        st.caption("匯出保留全部查詢記錄及差異證據。標準全文和上傳文件請按適用授權處理；ISO Open Data 與標準全文的使用條款不同。")
