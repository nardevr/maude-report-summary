"""End-to-end runs of cli.run against a fake openFDA session (no network)."""

import json
import re
import sys
from datetime import date, datetime, timezone

from conftest import FakeSession, load_fixture
from maude_summary import cli

NOW = datetime(2026, 11, 2, 15, 0, 0, tzinfo=timezone.utc)


def _run(tmp_path, session, no_llm=True):
    return cli.run("GZF", date(2021, 1, 1), tmp_path, no_llm, session=session, sleep=lambda s: None, now=NOW)


def test_full_run_from_recorded_records(tmp_path):
    fx = load_fixture("gzf_records_trimmed.json")
    code = _run(tmp_path, FakeSession(fx["results"]))
    assert code == 0
    for name in ["report.md", "monthly_counts.csv", "event_type_by_year.csv", "top_manufacturers.csv", "monthly.png", "raw/manifest.json"]:
        assert (tmp_path / name).exists(), name
    report = (tmp_path / "report.md").read_text()
    assert "| Totals check | MATCH: sum of monthly counts 7 = search total (`meta.results.total`) 7 = raw records fetched 7" in report
    assert "| Event-type check | MATCH" in report
    assert "2026-11-02 15:00:00 UTC / 2026-11-02 23:00:00 Singapore" in report
    assert "| openFDA last updated | 2026-10-13" in report
    assert "| Generic name | PERIPHERAL NERVE STIMULATOR" in report
    assert "| STIMWAVE TECHNOLOGIES INC. | 6 |" in report
    assert "A report can name more than one manufacturer, so this column can sum to more than the total." in report
    assert "Names appear as reported, so one company can show up on more than one row." in report
    assert "Data provided by the U.S. Food and Drug Administration." in report
    assert "Themes not generated" in report
    assert not re.search(r"\b(caused|dangerous|safest)\b", report, re.I)
    manifest = json.loads((tmp_path / "raw/manifest.json").read_text())
    assert [m["file"] for m in manifest] == ["records_page01_skip0.json.gz", "count_date_received.json.gz", "count_event_type_exact.json.gz"]
    assert all((tmp_path / "raw" / m["file"]).exists() for m in manifest)
    assert (tmp_path / "top_manufacturers.csv").read_text().splitlines()[0] == "manufacturer,unique_reports"


def test_empty_result(tmp_path):
    code = _run(tmp_path, FakeSession([]))
    assert code == 0
    report = (tmp_path / "report.md").read_text()
    assert "No reports found for product code GZF in this window." in report
    assert "| Totals check | MATCH: sum of monthly counts 0 = search total (`meta.results.total`) 0 = raw records fetched 0" in report
    assert "| (no device entry with product code GZF) | 0 |" in report


def test_api_error_gives_error_line_not_crash(tmp_path):
    fx = load_fixture("gzf_records_trimmed.json")
    code = _run(tmp_path, FakeSession(fx["results"], fail={"records": 500}))
    assert code == 1
    report = (tmp_path / "report.md").read_text()
    assert "**Errors in this run (the report is partial):**" in report
    assert "**Section failed: event type per year.**" in report
    assert "**Section failed: manufacturers.**" in report
    assert "| Totals check | NOT AVAILABLE" in report
    # the monthly section still rendered from its own query
    assert "![Reports received per month](monthly.png)" in report
    manifest = json.loads((tmp_path / "raw/manifest.json").read_text())
    assert manifest[0]["http_status"] == 500 and manifest[0]["attempts"] == 4


def test_monthly_query_error_gives_error_line(tmp_path):
    fx = load_fixture("gzf_records_trimmed.json")
    code = _run(tmp_path, FakeSession(fx["results"], fail={"count_date_received": 503}))
    assert code == 1
    report = (tmp_path / "report.md").read_text()
    assert "**Section failed: reports per month.**" in report
    assert "| STIMWAVE TECHNOLOGIES INC. | 6 |" in report


def test_package_does_not_import_anthropic():
    assert "anthropic" not in sys.modules
