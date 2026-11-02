"""openFDA device adverse event calls: retry with backoff, and save every raw response.

Every response used in the report is written gzipped to ``<out>/raw/`` exactly as
received (body bytes unchanged), and listed in ``<out>/raw/manifest.json`` with its
query URL (any api_key removed), fetch timestamp (UTC), HTTP status and record count.
"""

from __future__ import annotations

import gzip
import json
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

BASE_URL = "https://api.fda.gov/device/event.json"

# Keyless openFDA refuses limit=1000 (HTTP 403 API_KEY_MISSING, observed in Phase 0),
# so pages are 999 records. skip may not exceed 25000.
PAGE_LIMIT = 999
SKIP_MAX = 25000
MAX_RECORDS = (SKIP_MAX // PAGE_LIMIT + 1) * PAGE_LIMIT  # 26 pages -> 25,974 records

BACKOFF_SECONDS = (1, 2, 4)  # waits between attempts: 4 attempts in total
TIMEOUT_SECONDS = 120
RETRY_STATUSES = {429, 500, 502, 503, 504}


class FetchError(Exception):
    """A request that failed after all retries, or returned an unusable response."""


def redact(url: str) -> str:
    """Remove any api_key parameter from a URL before it is saved or shown."""
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k.lower() != "api_key"]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query, safe=":[]")))


def build_url(params: dict[str, str]) -> str:
    return BASE_URL + "?" + urllib.parse.urlencode(params, safe=":[]")


def search_expr(product_code: str, since: str, until: str) -> str:
    """openFDA search for one product code and a date_received window (dates as YYYYMMDD)."""
    return f"device.device_report_product_code:{product_code} AND date_received:[{since} TO {until}]"


@dataclass
class Fetcher:
    """Performs GETs with retry and records each raw response in ``raw_dir``.

    ``session`` needs a ``get(url, timeout=...)`` method returning an object with
    ``status_code`` and ``content`` (a ``requests.Session`` or a test double).
    """

    raw_dir: Path
    session: object = None
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    manifest: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.raw_dir = Path(self.raw_dir)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        if self.session is None:
            import requests

            self.session = requests.Session()

    def get(self, name: str, params: dict[str, str]) -> dict:
        """GET the query and return parsed JSON.

        openFDA answers "no matches" with HTTP 404 NOT_FOUND; that is returned as an
        empty result (``{"meta": None, "results": []}``), not an error.
        Raises FetchError after retries are exhausted or on any other non-200 status.
        """
        url = build_url(params)
        attempts = 0
        last_error = ""
        while True:
            attempts += 1
            fetched = self.now().isoformat(timespec="seconds")
            try:
                resp = self.session.get(url, timeout=TIMEOUT_SECONDS)
                status, body = resp.status_code, resp.content
            except Exception as exc:  # network failure: retry
                status, body = None, b""
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if status == 200 or status not in RETRY_STATUSES:
                    break
                last_error = f"HTTP {status}"
            if attempts > len(BACKOFF_SECONDS):
                self._record(name, url, fetched, status, body, attempts, error=last_error)
                raise FetchError(f"{name}: failed after {attempts} attempts ({last_error})")
            self.sleep(BACKOFF_SECONDS[attempts - 1])

        try:
            data = json.loads(body) if body else {}
        except ValueError:
            self._record(name, url, fetched, status, body, attempts, error="invalid JSON")
            raise FetchError(f"{name}: HTTP {status}, response is not valid JSON")

        if status == 404 and data.get("error", {}).get("code") == "NOT_FOUND":
            self._record(name, url, fetched, status, body, attempts, records=0)
            return {"meta": None, "results": []}
        if status != 200:
            err = data.get("error", {}) if isinstance(data, dict) else {}
            msg = f"HTTP {status} {err.get('code', '')} {err.get('message', '')}".strip()
            self._record(name, url, fetched, status, body, attempts, error=msg)
            raise FetchError(f"{name}: {msg}")

        results = data.get("results", [])
        total = ((data.get("meta") or {}).get("results") or {}).get("total")
        self._record(name, url, fetched, status, body, attempts, records=len(results), meta_total=total)
        return data

    def _record(self, name, url, fetched, status, body, attempts, records=None, meta_total=None, error=None):
        fname = f"{name}.json.gz"
        with gzip.open(self.raw_dir / fname, "wb") as fh:
            fh.write(body or b"")
        entry = {
            "file": fname,
            "url": redact(url),
            "fetched_utc": fetched,
            "http_status": status,
            "attempts": attempts,
            "records_in_response": records,
        }
        if meta_total is not None:
            entry["meta_results_total"] = meta_total
        if error:
            entry["error"] = error
        self.manifest.append(entry)

    def write_manifest(self) -> Path:
        path = self.raw_dir / "manifest.json"
        path.write_text(json.dumps(self.manifest, indent=2) + "\n")
        return path


def fetch_records(fetcher: Fetcher, product_code: str, since: str, until: str) -> tuple[list[dict], dict | None, int]:
    """Fetch every raw record for the code and window with skip paging and a stable sort.

    Returns (records, meta of the first page, meta.results.total). Raises FetchError if
    a page fails, or if the total exceeds what keyless skip paging can reach (no
    search-after paging, by spec).
    """
    search = search_expr(product_code, since, until)
    records: list[dict] = []
    first_meta = None
    total = 0
    skip = 0
    page = 1
    while True:
        data = fetcher.get(
            f"records_page{page:02d}_skip{skip}",
            {"search": search, "sort": "date_received:asc", "limit": str(PAGE_LIMIT), "skip": str(skip)},
        )
        if page == 1:
            first_meta = data.get("meta")
            total = ((first_meta or {}).get("results") or {}).get("total", 0)
            if total > MAX_RECORDS:
                raise FetchError(
                    f"search total {total:,} exceeds the {MAX_RECORDS:,} records reachable with keyless "
                    f"skip paging; use a later --since date"
                )
        batch = data.get("results", [])
        records.extend(batch)
        skip += PAGE_LIMIT
        if not batch or skip >= total:
            break
        page += 1
    return records, first_meta, total


def fetch_daily_counts(fetcher: Fetcher, product_code: str, since: str, until: str) -> list[dict]:
    """count=date_received: one row per day ({"time": "YYYYMMDD", "count": n})."""
    data = fetcher.get("count_date_received", {"search": search_expr(product_code, since, until), "count": "date_received"})
    return data.get("results", [])


def fetch_event_type_counts(fetcher: Fetcher, product_code: str, since: str, until: str) -> list[dict]:
    """count=event_type.exact (count=event_type returns HTTP 500 live; .exact is the keyword field)."""
    data = fetcher.get("count_event_type_exact", {"search": search_expr(product_code, since, until), "count": "event_type.exact"})
    return data.get("results", [])
