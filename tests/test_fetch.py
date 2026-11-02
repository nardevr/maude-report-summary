import gzip
import json

import pytest

from conftest import FakeSession, load_fixture
from maude_summary import fetch
from maude_summary.fetch import FetchError, Fetcher


def test_retry_backoff_then_error(tmp_path):
    sleeps = []
    f = Fetcher(raw_dir=tmp_path, session=FakeSession([], fail={"records": 500}), sleep=sleeps.append)
    with pytest.raises(FetchError, match="failed after 4 attempts"):
        f.get("x", {"search": "a"})
    assert sleeps == [1, 2, 4]
    assert f.manifest[-1]["http_status"] == 500 and f.manifest[-1]["attempts"] == 4
    assert "error" in f.manifest[-1]


def test_network_exception_is_retried(tmp_path):
    sleeps = []
    f = Fetcher(raw_dir=tmp_path, session=FakeSession([], fail={"records": ConnectionError("down")}), sleep=sleeps.append)
    with pytest.raises(FetchError, match="ConnectionError"):
        f.get("x", {"search": "a"})
    assert sleeps == [1, 2, 4]


def test_not_found_is_an_empty_result_not_an_error(tmp_path):
    f = Fetcher(raw_dir=tmp_path, session=FakeSession([]), sleep=lambda s: None)
    assert f.get("x", {"search": "a"}) == {"meta": None, "results": []}
    assert f.manifest[-1]["http_status"] == 404 and f.manifest[-1]["records_in_response"] == 0


def test_raw_response_saved_gzipped_with_manifest(tmp_path):
    fx = load_fixture("gzf_records_trimmed.json")
    f = Fetcher(raw_dir=tmp_path, session=FakeSession(fx["results"]), sleep=lambda s: None)
    recs, meta, total = fetch.fetch_records(f, "GZF", "20210101", "20261102")
    assert total == len(recs) == len(fx["results"])
    f.write_manifest()
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    e = manifest[0]
    assert e["file"] == "records_page01_skip0.json.gz"
    assert "sort=date_received:asc" in e["url"] and "limit=999" in e["url"]
    saved = json.loads(gzip.open(tmp_path / e["file"]).read())
    assert len(saved["results"]) == len(recs)
    assert set(e) >= {"url", "fetched_utc", "http_status", "records_in_response"}


def test_paging_over_several_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "PAGE_LIMIT", 3)
    recs = load_fixture("gzf_records_trimmed.json")["results"]  # 7 records -> pages of 3, 3, 1
    s = FakeSession(recs)
    f = Fetcher(raw_dir=tmp_path, session=s, sleep=lambda x: None)
    got, _, total = fetch.fetch_records(f, "GZF", "20210101", "20261102")
    assert [r["mdr_report_key"] for r in got] == [r["mdr_report_key"] for r in recs]
    assert [m["file"] for m in f.manifest] == ["records_page01_skip0.json.gz", "records_page02_skip3.json.gz", "records_page03_skip6.json.gz"]


def test_total_above_keyless_paging_cap_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "MAX_RECORDS", 5)
    recs = load_fixture("gzf_records_trimmed.json")["results"]
    f = Fetcher(raw_dir=tmp_path, session=FakeSession(recs), sleep=lambda x: None)
    with pytest.raises(FetchError, match="later --since"):
        fetch.fetch_records(f, "GZF", "20210101", "20261102")


def test_redact_removes_api_key():
    assert "api_key" not in fetch.redact("https://api.fda.gov/device/event.json?search=a:b&api_key=SECRET&limit=1")
    assert "SECRET" not in fetch.redact("https://api.fda.gov/device/event.json?api_key=SECRET")
