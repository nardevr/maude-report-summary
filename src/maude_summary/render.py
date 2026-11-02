"""Render report.md, the CSV tables and monthly.png. Renders only numbers computed in analyze.py."""

from __future__ import annotations

import numbers
from pathlib import Path

import pandas as pd

TOP_N_MANUFACTURERS = 10
CREDIT_LINE = "Data provided by the U.S. Food and Drug Administration."
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

LIMITATIONS_MD = """\
From openFDA (<https://open.fda.gov/apis/device/event/>):

- "a causal relationship cannot be established between product and reactions listed in a report."
- "Adverse event reports submitted to FDA do not undergo extensive validation or verification."
- "potential under-reporting of events and lack of information about frequency of device use."

From FDA's MAUDE page (<https://www.fda.gov/medical-devices/mandatory-reporting-requirements-manufacturers-importers-and-device-user-facilities/manufacturer-and-user-facility-device-experience-database-maude>):

- MDR (Medical Device Report) data "is not intended to be used either to evaluate rates of adverse events" or "to compare adverse event occurrence rates across devices."

In addition: report counts reflect reporting behaviour (for example manufacturer reporting
practice, recalls and publicity) as well as events. Duplicate reports can occur."""


def fmt(n) -> str:
    return "n/a" if n is None else f"{int(n):,}"


def md_table(df: pd.DataFrame, right_align_from: int = 1) -> str:
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |",
             "|" + "|".join(":---" if i < right_align_from else "---:" for i in range(len(cols))) + "|"]
    for row in df.itertuples(index=False):
        cells = [fmt(v) if isinstance(v, numbers.Integral) else str(v) for v in row]
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    return "\n".join(lines)


def monthly_grid(monthly: pd.DataFrame) -> pd.DataFrame:
    """Year x month grid of the monthly counts, for a compact table. Months outside the data are blank."""
    grid = {}
    for month, n in zip(monthly["month"], monthly["reports"]):
        year, mm = month.split("-")
        grid.setdefault(year, [""] * 12)[int(mm) - 1] = fmt(n)
    df = pd.DataFrame([[y, *vals] for y, vals in sorted(grid.items())], columns=["Year", *MONTHS])
    totals = monthly.assign(year=monthly["month"].str[:4]).groupby("year")["reports"].sum()
    df["Total"] = [fmt(totals[y]) for y in df["Year"]]
    return df


def write_monthly_png(monthly: pd.DataFrame, product_code: str, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 4), dpi=120)
    x = pd.PeriodIndex(monthly["month"], freq="M").to_timestamp()
    ax.bar(x, monthly["reports"], width=25, color="#3b6ea5")
    ax.set_title(f"MAUDE reports received per month, product code {product_code}")
    ax.set_ylabel("Reports received")
    ax.set_xlabel("Month received by FDA (date_received)")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", alpha=0.3)
    fig.text(0.01, 0.01, "Source: openFDA device/event, count=date_received. Report counts, not rates.", fontsize=7, color="#555")
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(path)
    plt.close(fig)


def _failed(section: str, error: str) -> str:
    return f"**Section failed: {section}.** {error}"


def write_outputs(res: dict, out: Path) -> Path:
    """Write CSVs, chart and report.md into ``out``. ``res`` is the run result built in cli.run."""
    out = Path(out)
    code = res["product_code"]
    lines: list[str] = []
    add = lines.append

    monthly = res.get("monthly")
    if monthly is not None:
        monthly.to_csv(out / "monthly_counts.csv", index=False)
        if len(monthly):
            write_monthly_png(monthly, code, out / "monthly.png")
    if res.get("event_by_year") is not None:
        res["event_by_year"].to_csv(out / "event_type_by_year.csv", index=False)
    if res.get("manufacturers") is not None:
        res["manufacturers"].to_csv(out / "top_manufacturers.csv", index=False)

    cross = res["cross_check"]
    if cross["status"] == "NOT AVAILABLE":
        cross_txt = "NOT AVAILABLE (a required section failed, see errors below)"
    else:
        rel = "=" if cross["status"] == "MATCH" else "vs"
        cross_txt = (f"{cross['status']}: sum of monthly counts {fmt(cross['monthly_sum'])} {rel} search total "
                     f"(`meta.results.total`) {fmt(cross['search_total'])} {rel} raw records fetched "
                     f"{fmt(cross['records_fetched'])} ({fmt(cross['unique_report_keys'])} unique `mdr_report_key`)")
    ev = res.get("event_check")
    if ev is None:
        ev_txt = "NOT AVAILABLE (a required section failed, see errors below)"
    elif ev["ok"]:
        ev_txt = "MATCH: window totals from the raw-record pivot equal `count=event_type.exact`"
    else:
        ev_txt = (f"MISMATCH: raw-record pivot {ev['from_records']} vs `count=event_type.exact` {ev['from_count_query']}")

    add(f"# MAUDE report summary: product code {code}")
    add("")
    add("| Field | Value |")
    add("|:---|:---|")
    add(f"| Product code | {code} |")
    add(f"| Generic name | {res.get('generic_name') or 'n/a'} (most frequent `device.generic_name` on matching device entries) |")
    add(f"| Query window | `date_received` {res['since_iso']} to {res['until_iso']} |")
    add(f"| openFDA last updated | {res.get('last_updated') or 'n/a'} (`meta.last_updated`) |")
    add(f"| Run timestamp | {res['run_utc']} UTC / {res['run_sgt']} Singapore (UTC+8) |")
    add(f"| Totals check | {cross_txt} |")
    add(f"| Event-type check | {ev_txt} |")
    add("")
    if res["errors"]:
        add("**Errors in this run (the report is partial):**")
        add("")
        for e in res["errors"]:
            add(f"- {e}")
        add("")
    if res.get("search_total") == 0:
        add(f"No reports found for product code {code} in this window.")
        add("")

    add("## 1. Reports received per month")
    add("")
    if monthly is None:
        add(_failed("reports per month", res.get("daily_error", "")))
    elif not len(monthly):
        add("No reports in this window.")
    else:
        add("![Reports received per month](monthly.png)")
        add("")
        add(md_table(monthly_grid(monthly)))
        add("")
        add(f"Source: `raw/count_date_received.json.gz` (`count=date_received`), aggregated to calendar months. "
            f"Last month with data: {monthly['month'].iloc[-1]}; the latest month may be incomplete. "
            f"Full series in `monthly_counts.csv`.")
    add("")

    add("## 2. Reports by event type per year")
    add("")
    eby = res.get("event_by_year")
    if eby is None:
        add(_failed("event type per year", res.get("records_error", "")))
    elif not len(eby):
        add("No reports in this window.")
    else:
        add(md_table(eby.rename(columns={"year": "Year"})))
        add("")
        add("Each report counts once, under the `event_type` value as reported, by year of `date_received`. "
            "Source: the raw record pages in `raw/` (`records_page*.json.gz`), cross-checked against "
            "`raw/count_event_type_exact.json.gz`.")
    add("")

    add("## 3. Top manufacturers by number of unique reports")
    add("")
    mf = res.get("manufacturers")
    if mf is None:
        add(_failed("manufacturers", res.get("records_error", "")))
    else:
        named, no_match = mf.iloc[:-1], mf.iloc[-1:]
        shown = pd.concat([named.head(TOP_N_MANUFACTURERS), no_match])
        add(md_table(shown.rename(columns={"manufacturer": "Manufacturer (as reported)",
                                          "unique_reports": "Unique reports"})))
        add("")
        add("Names appear as reported, so one company can show up on more than one row.")
        add("")
        add("A report can name more than one manufacturer, so this column can sum to more than the total.")
        add("")
        add(f"Unique reports = distinct `mdr_report_key` naming the manufacturer (`manufacturer_d_name`, trimmed and "
            f"upper-cased) on a device entry with product code {code}. These are report counts, not rates, and not a "
            f"ranking of safety. Source: the raw record pages in `raw/`.")
        if len(named) > TOP_N_MANUFACTURERS:
            add("")
            add(f"{fmt(len(named) - TOP_N_MANUFACTURERS)} further manufacturer names are listed in `top_manufacturers.csv`.")
    add("")

    add("## 4. Narrative themes")
    add("")
    add(res["themes_line"])
    add("")

    add("## 5. Limitations")
    add("")
    add(LIMITATIONS_MD)
    add("")
    add("---")
    add("")
    add(f"{CREDIT_LINE} Source: openFDA device adverse event API (<https://open.fda.gov/apis/device/event/>). "
        "Every raw response used is saved in `raw/` and listed in `raw/manifest.json` with its query URL and fetch time.")
    add("")
    add("Independent personal project, not affiliated with or endorsed by FDA. Not medical advice.")
    add("")

    path = out / "report.md"
    path.write_text("\n".join(lines))
    return path
