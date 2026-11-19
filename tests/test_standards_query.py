import unittest
from datetime import date, datetime
from unittest.mock import patch

from modules.standards_query import QuerySpec, default_query, parse_query


class StandardsQueryTests(unittest.TestCase):
    TODAY = date(2026, 11, 19)

    def test_rolling_calendar_year_and_hong_kong_clock(self):
        with patch("modules.standards_query.hong_kong_today", return_value=self.TODAY):
            query = default_query()
        self.assertEqual(query.start_date, date(2025, 11, 19))
        self.assertEqual(query.end_date, self.TODAY)
        self.assertEqual(default_query(date(2024, 2, 29)).start_date, date(2023, 2, 28))
        self.assertEqual(default_query(date(2026, 1, 1)).start_date, date(2025, 1, 1))

    def test_offline_english_and_chinese_scope(self):
        query = parse_query("查過去一年 ISO 13485:2016及 ISO 10993-1 的更新", today=self.TODAY)
        self.assertEqual(query.standards, ("ISO 13485", "ISO 10993-1"))
        self.assertEqual(query.start_date, date(2025, 11, 19))
        query = parse_query("Find ISO / IEC 27001 updates in the past 6 months", today=self.TODAY)
        self.assertEqual(query.standards, ("ISO/IEC 27001",))
        self.assertEqual(query.start_date, date(2026, 5, 19))
        self.assertEqual(parse_query("最近一個月所有ISO", today=date(2026, 3, 31)).start_date, date(2026, 2, 28))

    def test_explicit_scope_and_dates(self):
        query = parse_query("All ISO from 2025-01-01 to 2025-12-31 ICS 11.040", today=self.TODAY)
        self.assertEqual(query.ics_prefixes, ("11.040",))
        self.assertEqual(query.end_date, date(2025, 12, 31))
        query = parse_query('ISO/TC 210 keyword: "medical devices"', today=self.TODAY)
        self.assertEqual(query.committees, ("ISO/TC 210",))
        self.assertEqual(query.keywords, ("medical devices",))
        query = parse_query("ISO/IEC JTC1/SC27 past year", today=self.TODAY)
        self.assertEqual(query.committees, ("ISO/IEC JTC 1/SC 27",))

    def test_dates_do_not_become_standard_numbers_and_compact_refs_normalize(self):
        query = parse_query("all ISO 2025-11-19 to 2026-11-19", today=self.TODAY)
        self.assertEqual(query.standards, ())
        self.assertEqual(query.start_date, date(2025, 11, 19))
        self.assertEqual(parse_query("ISO9001 past year", today=self.TODAY).standards, ("ISO 9001",))
        query = QuerySpec.from_dict({**default_query(self.TODAY).to_dict(), "standards": ["ISO9001"],
                                    "committees": ["ISO/TC210"]}, today=self.TODAY)
        self.assertEqual(query.standards, ("ISO 9001",))
        self.assertEqual(query.committees, ("ISO/TC 210",))

    def test_unsupported_scope_qualifiers_never_silently_change_search(self):
        for request in ("ISO 13485 and 14971 past year", "過去一年 ISO 9001 和 13485",
                        "ISO 10993 series past year", "ISO 9001 except ISO 13485 past year",
                        "all ISO medical devices past year", "ISO 9001 about manufacturing past year"):
            with self.subTest(request=request), self.assertRaises(ValueError):
                parse_query(request, today=self.TODAY)
        self.assertEqual(parse_query("ISO 10993-* series past year", today=self.TODAY).standards, ("ISO 10993-*",))

    def test_never_silently_broaden_missing_scope_or_unsupported_publisher(self):
        for request in ("find recent updates", "medical device standards", "IEC 60601 past year",
                        "ASTM F1980 last year", "EN ISO 13485 past year", "all ISO and IEC updates"):
            with self.subTest(request=request), self.assertRaises(ValueError):
                parse_query(request, today=self.TODAY)

    def test_ambiguous_or_malformed_dates_rejected(self):
        for request in ("ISO 9001 since 2025-01-01", "ISO 9001 2025-1-1 to 2025-12-31",
                        "ISO 9001 2025-01-01 2025-12-31 2026-01-01", "ISO 9001 last quarter",
                        "ISO 9001 past 0 months", "ISO 9001 2026-01-01 to 2027-01-01"):
            with self.subTest(request=request), self.assertRaises(ValueError):
                parse_query(request, today=self.TODAY)

    def test_schema_roundtrip_and_strict_validation(self):
        payload = default_query(self.TODAY).to_dict()
        self.assertEqual(QuerySpec.from_dict(payload, today=self.TODAY), default_query(self.TODAY))
        invalid = [{**payload, "start_date": "2025-02-29"}, {**payload, "end_date": "2027-01-01"},
                   {**payload, "standards": "ISO 9001"}, {**payload, "standards": [True]},
                   {**payload, "publisher": "IEC"}, {**payload, "action": "delete"},
                   {**payload, "start_date": "2026-12-01"}, {**payload, "ics_prefixes": ["medical"]},
                   {**payload, "standards": ["ISO 9001"] * 101}, "{}", None]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                QuerySpec.from_dict(value, today=self.TODAY)
        with self.assertRaises(ValueError):
            default_query(datetime(2026, 11, 19))

    def test_ai_keeps_scope_and_dates_and_sends_only_query(self):
        expected = parse_query("ISO 9001 past year", today=self.TODAY)
        prompts = []
        def complete(prompt):
            prompts.append(prompt)
            return expected.to_dict()
        result = parse_query("ISO 9001 past year", today=self.TODAY, use_ai=True, completion=complete)
        self.assertEqual(result, expected)
        self.assertIn('"today": "2026-11-19"', prompts[0])
        self.assertIn('"timezone": "Asia/Hong_Kong"', prompts[0])

    def test_ai_invented_or_omitted_filters_and_dates_rejected(self):
        payload = parse_query("ISO 9001 past year", today=self.TODAY).to_dict()
        for mutation in ({"standards": []}, {"standards": ["ISO 13485"]}, {"ics_prefixes": ["11.040"]},
                         {"keywords": ["medical devices"]}, {"start_date": "2025-01-01"},
                         {"notes": "invalid JSON fallback"}, {"publisher": "IEC"}):
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                parse_query("ISO 9001 past year", today=self.TODAY, use_ai=True,
                            completion=lambda _: {**payload, **mutation})

    def test_ai_literal_topic_does_not_invent_ics(self):
        payload = {**default_query(self.TODAY).to_dict(), "keywords": ["medical devices"]}
        query = parse_query("recent ISO publications about medical devices", today=self.TODAY,
                            use_ai=True, completion=lambda _: payload)
        self.assertEqual(query.keywords, ("medical devices",))
        with self.assertRaises(ValueError):
            parse_query("recent updates", today=self.TODAY, use_ai=True,
                        completion=lambda _: default_query(self.TODAY).to_dict())
        with self.assertRaises(ValueError):
            parse_query("ISO publications about medical devices", today=self.TODAY, use_ai=True,
                        completion=lambda _: {**payload, "start_date": "2020-01-01"})
        with self.assertRaises(ValueError):
            parse_query("all ISO medical devices past year", today=self.TODAY, use_ai=True,
                        completion=lambda _: default_query(self.TODAY).to_dict())


if __name__ == "__main__":
    unittest.main()
