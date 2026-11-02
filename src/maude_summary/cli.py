"""Command-line entry point: python -m maude_summary --product-code XXX --since YYYY-MM-DD --out DIR [--no-llm]."""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from maude_summary import analyze, llm, render
from maude_summary.fetch import FetchError, Fetcher, fetch_daily_counts, fetch_event_type_counts, fetch_records

SGT = timezone(timedelta(hours=8))


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m maude_summary",
                                description="One-page summary of openFDA MAUDE reports for one device product code.")
    p.add_argument("--product-code", required=True, help="FDA device product code, three letters (e.g. GZF)")
    p.add_argument("--since", default="2021-01-01", help="first date_received to include, YYYY-MM-DD (default 2021-01-01)")
    p.add_argument("--out", help="output directory (default reports/<CODE>)")
    p.add_argument("--no-llm", action="store_true", help="skip the narrative themes step")
    p.add_argument("--llm-sample", type=int, default=llm.DEFAULT_SAMPLE,
                   help=f"number of most recent reports with narrative text sent to the model "
                        f"(default {llm.DEFAULT_SAMPLE}, maximum {llm.MAX_SAMPLE})")
    args = p.parse_args(argv)
    args.product_code = args.product_code.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", args.product_code):
        p.error("--product-code must be three letters")
    try:
        args.since_date = date.fromisoformat(args.since)
    except ValueError:
        p.error("--since must be a date in YYYY-MM-DD form")
    if not 1 <= args.llm_sample <= llm.MAX_SAMPLE:
        p.error(f"--llm-sample must be between 1 and {llm.MAX_SAMPLE}")
    args.out = Path(args.out) if args.out else Path("reports") / args.product_code
    return args


def run(product_code: str, since: date, out: Path, no_llm: bool, *, llm_sample: int = llm.DEFAULT_SAMPLE,
        session=None, sleep=None, now: datetime | None = None, llm_call=None) -> int:
    """Fetch, analyse and render. Returns 0 if every section succeeded, 1 if the report is partial."""
    now = now or datetime.now(timezone.utc)
    until = now.date()
    if since > until:
        raise SystemExit("--since is after today")
    s, u = since.strftime("%Y%m%d"), until.strftime("%Y%m%d")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    kwargs = {"raw_dir": out / "raw", "session": session}
    if sleep is not None:
        kwargs["sleep"] = sleep
    fetcher = Fetcher(**kwargs)

    res: dict = {
        "product_code": product_code, "since_iso": since.isoformat(), "until_iso": until.isoformat(),
        "run_utc": now.strftime("%Y-%m-%d %H:%M:%S"), "run_sgt": now.astimezone(SGT).strftime("%Y-%m-%d %H:%M:%S"),
        "errors": [],
    }

    records = None
    try:
        records, meta, total = fetch_records(fetcher, product_code, s, u)
        res["search_total"] = total
        res["last_updated"] = (meta or {}).get("last_updated")
        res["generic_name"] = analyze.most_common_generic_name(records, product_code)
        res["event_by_year"] = analyze.event_type_by_year(records)
        res["manufacturers"] = analyze.manufacturer_unique_reports(records, product_code)
    except FetchError as exc:
        res["records_error"] = f"Raw records could not be fetched: {exc}"
        res["errors"].append(f"Raw records (event type per year, manufacturers, totals check): {exc}")
        records = None

    try:
        res["monthly"] = analyze.monthly_counts(fetch_daily_counts(fetcher, product_code, s, u))
    except FetchError as exc:
        res["monthly"] = None
        res["daily_error"] = f"count=date_received could not be fetched: {exc}"
        res["errors"].append(f"Reports per month (count=date_received): {exc}")

    if records is not None:
        try:
            res["event_check"] = analyze.event_type_totals_check(records, fetch_event_type_counts(fetcher, product_code, s, u))
        except FetchError as exc:
            res["errors"].append(f"Event-type cross-check (count=event_type.exact): {exc}")

    res["cross_check"] = analyze.totals_cross_check(res["monthly"], res.get("search_total"), records)
    res["themes"] = themes_step(records, no_llm, llm_sample, out, fetcher.manifest, llm_call)

    fetcher.write_manifest()
    report = render.write_outputs(res, out)
    print(f"Report written: {report}")
    print(f"Totals check: {res['cross_check']['status']}")
    for e in res["errors"]:
        print(f"ERROR: {e}", file=sys.stderr)
    if res["themes"].status != "ok" and not no_llm:
        print(f"NOTE: {res['themes'].line}", file=sys.stderr)
    return 1 if res["errors"] else 0


def themes_step(records, no_llm, sample_size, out, manifest, call) -> llm.ThemesResult:
    if no_llm:
        return llm.ThemesResult("skipped", llm.NO_LLM_LINE)
    if records is None:
        return llm.ThemesResult("unavailable", "Themes unavailable: raw records could not be fetched.")
    api_key = llm.get_api_key()
    if not api_key:
        return llm.ThemesResult("skipped", llm.NO_KEY_LINE)
    model = (os.environ.get(llm.MODEL_ENV) or "").strip() or llm.DEFAULT_MODEL
    return llm.generate_themes(records, sample_size=sample_size, api_key=api_key, model=model,
                               raw_dir=Path(out) / "raw", manifest=manifest, call=call)


def main(argv=None) -> int:
    args = parse_args(argv)
    return run(args.product_code, args.since_date, args.out, args.no_llm, llm_sample=args.llm_sample)
