"""Command-line interface to the same standards-update workflow used by Streamlit."""
from __future__ import annotations

import argparse
from pathlib import Path

from .app_paths import data_root
from .standards_catalogue import fetch_iso_catalogue, load_iso_catalogue
from .standards_query import QuerySpec, default_query, parse_query
from .standards_update_workflow import compare_discovered_pair, report_json, report_markdown, run_update_workflow


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="Inclusive publication date, YYYY-MM-DD")
    parser.add_argument("--until", help="Inclusive publication date, YYYY-MM-DD")
    parser.add_argument("--standard", action="append", default=[], help="Exact family or explicit series wildcard, e.g. ISO 10993-*")
    parser.add_argument("--ics", action="append", default=[], help="ICS prefix, e.g. 11.100.20")
    parser.add_argument("--committee", action="append", default=[])
    parser.add_argument("--keyword", action="append", default=[])
    parser.add_argument("--all-iso", action="store_true", help="Explicitly query all ISO publications")
    parser.add_argument("--request", help="Natural-language request instead of date/scope flags")
    parser.add_argument("--ai", action="store_true", help="Use the configured AI provider to plan --request")
    parser.add_argument("--snapshot", type=Path, help="Use local ISO-format JSONL; origin is labelled user-supplied")
    parser.add_argument("--output", type=Path, default=data_root() / "standards" / "reports")
    parser.add_argument("--update-id", type=int)
    parser.add_argument("--predecessor-id", type=int)
    parser.add_argument("--old-file", type=Path)
    parser.add_argument("--new-file", type=Path)
    args = parser.parse_args(argv)
    try:
        scope_set = bool(args.standard or args.ics or args.committee or args.keyword or args.all_iso)
        if args.request:
            if args.since or args.until or scope_set:
                raise ValueError("Use --request or explicit date/scope flags, not both.")
            query = parse_query(args.request, use_ai=args.ai)
        else:
            if args.ai:
                raise ValueError("--ai requires --request.")
            if not scope_set:
                raise ValueError("Specify --standard, --ics, --committee, --keyword or --all-iso.")
            if args.all_iso and (args.standard or args.ics or args.committee or args.keyword):
                raise ValueError("--all-iso cannot be combined with a narrower scope.")
            defaults = default_query()
            query = QuerySpec.from_dict({
                "publisher": "ISO", "start_date": args.since or defaults.start_date.isoformat(),
                "end_date": args.until or defaults.end_date.isoformat(), "standards": args.standard,
                "ics_prefixes": args.ics, "committees": args.committee, "keywords": args.keyword,
            })
        comparison_args = [args.update_id, args.predecessor_id, args.old_file, args.new_file]
        if any(item is not None for item in comparison_args) and not all(item is not None for item in comparison_args):
            raise ValueError("Comparison requires --update-id, --predecessor-id, --old-file and --new-file together.")
        if args.snapshot:
            snapshot = load_iso_catalogue(args.snapshot, source_url="user-supplied ISO-format JSONL (origin not independently verified)")
        else:
            snapshot = fetch_iso_catalogue(data_root() / "standards" / "catalogues")
        report = run_update_workflow(snapshot, query)
        if all(item is not None for item in comparison_args):
            compare_discovered_pair(report, snapshot, update_id=args.update_id, predecessor_id=args.predecessor_id,
                                    old_path=args.old_file, new_path=args.new_file)
        args.output.mkdir(parents=True, exist_ok=True)
        stem = f"standards-update-{report['run_id']}"
        json_path, md_path = args.output / f"{stem}.json", args.output / f"{stem}.md"
        json_path.write_text(report_json(report), encoding="utf-8")
        md_path.write_text(report_markdown(report), encoding="utf-8")
        print(f"Found {len(report['updates'])} publication records within the selected snapshot and scope.")
        print("Historical completeness is not guaranteed; missing full text means content differences remain unverified.")
        print(json_path)
        print(md_path)
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(2, f"Standards update run failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
