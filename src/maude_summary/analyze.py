"""Deterministic analysis. Every number in the report is computed here from openFDA responses."""

from __future__ import annotations

from collections import Counter, defaultdict

import pandas as pd

EVENT_TYPE_ORDER = ["Death", "Injury", "Malfunction", "Other"]
BLANK_EVENT = "(blank)"
MISSING_EVENT = "(missing)"
NOT_STATED = "(not stated)"


def no_match_label(product_code: str) -> str:
    return f"(no device entry with product code {product_code})"


def monthly_counts(daily: list[dict]) -> pd.DataFrame:
    """Aggregate count=date_received daily rows into calendar months.

    Months with no reports between the first and last month that have data are
    filled with zero. Returns columns: month (YYYY-MM), reports.
    """
    if not daily:
        return pd.DataFrame({"month": pd.Series(dtype=str), "reports": pd.Series(dtype="int64")})
    df = pd.DataFrame(daily)
    df["month"] = pd.to_datetime(df["time"], format="%Y%m%d").dt.to_period("M")
    by_month = df.groupby("month")["count"].sum()
    full = pd.period_range(by_month.index.min(), by_month.index.max(), freq="M")
    by_month = by_month.reindex(full, fill_value=0)
    return pd.DataFrame({"month": by_month.index.strftime("%Y-%m"), "reports": by_month.values.astype("int64")})


def _event_label(record: dict) -> str:
    if "event_type" not in record or record["event_type"] is None:
        return MISSING_EVENT
    value = str(record["event_type"]).strip()
    return value if value else BLANK_EVENT


def event_type_by_year(records: list[dict]) -> pd.DataFrame:
    """Pivot raw records: one row per year of date_received, one column per event_type value.

    Each record counts once. Columns are Death, Injury, Malfunction, Other, then any other
    value observed, as reported, then Total. Returns an empty frame for no records.
    """
    if not records:
        return pd.DataFrame(columns=["year", *EVENT_TYPE_ORDER, "Total"])
    rows = [{"year": str(r["date_received"])[:4], "event_type": _event_label(r)} for r in records]
    df = pd.DataFrame(rows)
    pivot = df.pivot_table(index="year", columns="event_type", aggfunc="size", fill_value=0)
    extras = sorted(c for c in pivot.columns if c not in EVENT_TYPE_ORDER)
    pivot = pivot.reindex(columns=EVENT_TYPE_ORDER + extras, fill_value=0).astype("int64")
    pivot["Total"] = pivot.sum(axis=1)
    pivot = pivot.reset_index()
    pivot.columns.name = None
    return pivot


def event_type_totals_check(records: list[dict], exact_counts: list[dict]) -> dict:
    """Compare window totals from the raw-record pivot with count=event_type.exact."""
    from_records = Counter(_event_label(r) for r in records)
    from_count = {(t["term"].strip() or BLANK_EVENT): t["count"] for t in exact_counts}
    return {"ok": dict(from_records) == from_count, "from_records": dict(from_records), "from_count_query": from_count}


def manufacturer_unique_reports(records: list[dict], product_code: str) -> pd.DataFrame:
    """Spec rule 9: number of unique mdr_report_key per manufacturer.

    Only device entries whose device_report_product_code equals the selected code count.
    The name is manufacturer_d_name trimmed and upper-cased, "(not stated)" if missing.
    A report naming two matching manufacturers counts once for each. Records with no
    matching device entry are counted on one final line. Rows are sorted by
    unique_reports (descending), then name; the no-match line is always last.
    """
    code = product_code.strip().upper()
    keys_by_mfr: dict[str, set] = defaultdict(set)
    no_match: set = set()
    for rec in records:
        key = str(rec["mdr_report_key"])
        matching = [d for d in rec.get("device") or [] if str(d.get("device_report_product_code") or "").strip().upper() == code]
        if not matching:
            no_match.add(key)
            continue
        for dev in matching:
            name = str(dev.get("manufacturer_d_name") or "").strip().upper() or NOT_STATED
            keys_by_mfr[name].add(key)
    rows = sorted(((m, len(k)) for m, k in keys_by_mfr.items()), key=lambda x: (-x[1], x[0]))
    rows.append((no_match_label(code), len(no_match)))
    return pd.DataFrame(rows, columns=["manufacturer", "unique_reports"]).astype({"unique_reports": "int64"})


def most_common_generic_name(records: list[dict], product_code: str) -> str | None:
    """Most frequent device.generic_name (upper-cased) among matching device entries, by unique report."""
    code = product_code.strip().upper()
    keys_by_name: dict[str, set] = defaultdict(set)
    for rec in records:
        for dev in rec.get("device") or []:
            if str(dev.get("device_report_product_code") or "").strip().upper() == code:
                name = str(dev.get("generic_name") or "").strip().upper()
                if name:
                    keys_by_name[name].add(str(rec["mdr_report_key"]))
    if not keys_by_name:
        return None
    return sorted(keys_by_name.items(), key=lambda x: (-len(x[1]), x[0]))[0][0]


def totals_cross_check(monthly: pd.DataFrame, search_total: int | None, records: list[dict] | None) -> dict:
    """Sum of monthly counts vs meta.results.total vs raw records fetched (and unique keys).

    Any input that is unavailable (a failed section) makes the check "not available", never a pass.
    """
    monthly_sum = int(monthly["reports"].sum()) if monthly is not None else None
    fetched = len(records) if records is not None else None
    unique = len({str(r["mdr_report_key"]) for r in records}) if records is not None else None
    values = [monthly_sum, search_total, fetched, unique]
    if any(v is None for v in values):
        status = "NOT AVAILABLE"
    elif len(set(values)) == 1:
        status = "MATCH"
    else:
        status = "MISMATCH"
    return {"status": status, "monthly_sum": monthly_sum, "search_total": search_total,
            "records_fetched": fetched, "unique_report_keys": unique}
