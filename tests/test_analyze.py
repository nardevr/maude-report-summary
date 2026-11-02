import pandas as pd

from conftest import load_fixture
from maude_summary import analyze


# --- monthly aggregation from daily counts -------------------------------------------------

def test_monthly_aggregation_from_daily_counts_fills_missing_month():
    daily = load_fixture("gzf_count_date_received_trimmed.json")["results"]
    m = analyze.monthly_counts(daily)
    assert list(m["month"]) == ["2021-01", "2021-02", "2021-03"]
    jan = sum(r["count"] for r in daily if r["time"].startswith("202101"))
    mar = sum(r["count"] for r in daily if r["time"].startswith("202103"))
    assert list(m["reports"]) == [jan, 0, mar]
    assert (jan, mar) == (14, 28)  # values in the recorded openFDA response
    assert int(m["reports"].sum()) == sum(r["count"] for r in daily)


def test_monthly_aggregation_empty():
    m = analyze.monthly_counts([])
    assert len(m) == 0 and list(m.columns) == ["month", "reports"]


# --- event-type pivot ----------------------------------------------------------------------

def test_event_type_pivot_by_year():
    recs = load_fixture("gzf_records_trimmed.json")["results"]
    p = analyze.event_type_by_year(recs).set_index("year")
    assert list(p.columns) == ["Death", "Injury", "Malfunction", "Other", "Total"]
    assert p.loc["2021"].to_dict() == {"Death": 2, "Injury": 2, "Malfunction": 2, "Other": 0, "Total": 6}
    assert p.loc["2024"].to_dict() == {"Death": 0, "Injury": 0, "Malfunction": 0, "Other": 1, "Total": 1}
    assert int(p["Total"].sum()) == len(recs)


def test_event_type_pivot_keeps_unlisted_values_as_reported():
    recs = [{"mdr_report_key": "1", "date_received": "20220101", "event_type": "No answer provided"},
            {"mdr_report_key": "2", "date_received": "20220102", "event_type": ""},
            {"mdr_report_key": "3", "date_received": "20220103", "event_type": "Injury"}]
    p = analyze.event_type_by_year(recs)
    assert list(p.columns) == ["year", "Death", "Injury", "Malfunction", "Other", "(blank)", "No answer provided", "Total"]
    assert p.iloc[0]["Total"] == 3


def test_event_type_totals_check():
    recs = load_fixture("gzf_records_trimmed.json")["results"]
    good = [{"term": "Death", "count": 2}, {"term": "Injury", "count": 2}, {"term": "Malfunction", "count": 2}, {"term": "Other", "count": 1}]
    assert analyze.event_type_totals_check(recs, good)["ok"]
    bad = [dict(t) for t in good]
    bad[1]["count"] = 3
    assert not analyze.event_type_totals_check(recs, bad)["ok"]


# --- manufacturer unique-report counts (spec rule 9) ---------------------------------------

def _mfr():
    recs = load_fixture("manufacturer_cases_synthetic.json")["results"]
    df = analyze.manufacturer_unique_reports(recs, "GZF")
    return df, dict(zip(df["manufacturer"], df["unique_reports"]))


def test_same_manufacturer_twice_in_one_report_counts_once():
    _, c = _mfr()
    # report 900001 has two CURONIX entries (case/whitespace differ); 900002 has one more
    assert c["CURONIX LLC"] == 2


def test_two_matching_manufacturers_count_once_each():
    _, c = _mfr()
    # report 900002 names CURONIX and NALU; NALU also appears in 900003
    assert c["NALU MEDICAL INC"] == 2
    assert c["CURONIX LLC"] == 2


def test_device_entry_with_other_product_code_is_ignored():
    _, c = _mfr()
    assert "OTHER CODE MAKER INC" not in c


def test_missing_or_blank_manufacturer_is_not_stated():
    _, c = _mfr()
    assert c["(not stated)"] == 2


def test_record_without_matching_device_is_one_line_last():
    df, c = _mfr()
    assert df.iloc[-1]["manufacturer"] == "(no device entry with product code GZF)"
    assert df.iloc[-1]["unique_reports"] == 1
    assert list(df.columns) == ["manufacturer", "unique_reports"]
    # sorted by count desc then name, before the no-match line
    assert list(df["manufacturer"][:-1]) == ["(not stated)", "CURONIX LLC", "NALU MEDICAL INC"]


def test_manufacturer_counts_on_real_records():
    recs = load_fixture("gzf_records_trimmed.json")["results"]
    _, c = (lambda df: (df, dict(zip(df["manufacturer"], df["unique_reports"]))))(analyze.manufacturer_unique_reports(recs, "GZF"))
    assert c == {"STIMWAVE TECHNOLOGIES INC.": 6, "CURONIX LLC": 1, "(no device entry with product code GZF)": 0}


# --- totals cross-check --------------------------------------------------------------------

def test_cross_check_match():
    recs = load_fixture("gzf_records_trimmed.json")["results"]
    monthly = pd.DataFrame({"month": ["2021-01", "2021-03"], "reports": [5, 2]})
    r = analyze.totals_cross_check(monthly, 7, recs)
    assert r == {"status": "MATCH", "monthly_sum": 7, "search_total": 7, "records_fetched": 7, "unique_report_keys": 7}


def test_cross_check_mismatch_is_reported():
    recs = load_fixture("gzf_records_trimmed.json")["results"]
    monthly = pd.DataFrame({"month": ["2021-01"], "reports": [8]})
    assert analyze.totals_cross_check(monthly, 7, recs)["status"] == "MISMATCH"
    # a duplicated record (e.g. paging drift) breaks the match on unique keys
    assert analyze.totals_cross_check(pd.DataFrame({"month": ["x"], "reports": [8]}), 8, recs + recs[:1])["status"] == "MISMATCH"


def test_cross_check_not_available_when_a_section_failed():
    assert analyze.totals_cross_check(None, 7, [])["status"] == "NOT AVAILABLE"
