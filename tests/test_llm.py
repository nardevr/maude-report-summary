"""LLM step: strict validation with a mocked model, key handling, sample selection. No network, no anthropic."""

import json
import sys
from datetime import date, datetime, timezone

import pytest

from conftest import FakeSession, load_fixture
from maude_summary import cli, llm

NOW = datetime(2026, 11, 2, 15, 0, 0, tzinfo=timezone.utc)
NARR = load_fixture("gzf_narratives_trimmed.json")["results"]  # 6 real records, 2 with narrative text
SAMPLE_KEYS = ["50949318", "50948697"]

GOOD = {"themes": [
    {"label": "Lead migration", "description": "Narratives describe a lead that moved from its intended position.",
     "report_keys": ["50949318", "50948697"]},
    {"label": "Skin irritation at site", "description": "Narratives describe redness or irritation where the device sat.",
     "report_keys": ["50948697"]},
]}


def mutate(fn):
    data = json.loads(json.dumps(GOOD))
    fn(data)
    return json.dumps(data)


def fake_call(text):
    calls = []

    def call(system, user, model, api_key):
        calls.append({"system": system, "user": user, "model": model, "api_key": api_key})
        return {"text": text, "stop_reason": "end_turn", "model": model, "usage": None, "request_id": "req_test"}

    call.calls = calls
    return call


def run_themes(tmp_path, text):
    manifest = []
    res = llm.generate_themes(NARR, sample_size=30, api_key="test-key-not-real", model="claude-haiku-5-5",
                              raw_dir=tmp_path / "raw", manifest=manifest, call=fake_call(text))
    return res, manifest


# --- each of these must produce the visible fallback (spec Phase 2 list) --------------------

FALLBACK_CASES = [
    ("digit in a label", mutate(lambda d: d["themes"][0].update(label="Lead migration 2")), llm.RULE_DIGITS),
    ("digit in a description", mutate(lambda d: d["themes"][0].update(description="Seen in 3 narratives.")), llm.RULE_DIGITS),
    ("6 themes", mutate(lambda d: d.update(themes=[dict(d["themes"][0], label=f"Theme {c}") for c in "abcdef"])), llm.RULE_COUNT),
    ("zero themes", json.dumps({"themes": []}), llm.RULE_COUNT),
    ("an extra key (in a theme)", mutate(lambda d: d["themes"][0].update(severity="high")), llm.RULE_KEYS),
    ("an extra key (top level)", mutate(lambda d: d.update(note="x")), llm.RULE_TOP),
    ("a missing key", mutate(lambda d: d["themes"][1].pop("description")), llm.RULE_KEYS),
    ("a wrong type (label)", mutate(lambda d: d["themes"][0].update(label=["Lead migration"])), llm.RULE_LABEL),
    ("a wrong type (report key)", mutate(lambda d: d["themes"][0].update(report_keys=[50949318])), llm.RULE_RKEYS),
    ("a report key not in the sample", mutate(lambda d: d["themes"][0]["report_keys"].append("50951794")), llm.RULE_IN_SAMPLE),
    ("malformed JSON", '{"themes": [{"label": "Lead migration", ', llm.RULE_JSON),
]


@pytest.mark.parametrize("name,text,rule", FALLBACK_CASES, ids=[c[0] for c in FALLBACK_CASES])
def test_invalid_output_gives_visible_fallback(tmp_path, name, text, rule):
    res, _ = run_themes(tmp_path, text)
    assert res.status == "unavailable"
    assert res.themes == []
    assert res.line == f"Themes unavailable: model output failed validation (failed rule: {rule})."


@pytest.mark.parametrize("text,rule", [
    ("[]", llm.RULE_TOP),
    (mutate(lambda d: d["themes"][0].update(label="")), llm.RULE_LABEL),
    (mutate(lambda d: d["themes"][0].update(label="x" * 61)), llm.RULE_LABEL),
    (mutate(lambda d: d["themes"][0].update(description="x" * 201)), llm.RULE_DESC),
    (mutate(lambda d: d["themes"][0].update(report_keys=[])), llm.RULE_RKEYS),
    (mutate(lambda d: d["themes"][0].update(report_keys=["50949318", "50949318"])), llm.RULE_RKEYS),
    ('{"themes": [], "themes": []}', llm.RULE_JSON),
    ("```json\n" + json.dumps(GOOD) + "\n```", llm.RULE_JSON),
])
def test_other_rule_boundaries(text, rule):
    with pytest.raises(llm.ValidationError) as err:
        llm.validate_output(text, set(SAMPLE_KEYS))
    assert err.value.rule == rule


def test_valid_output_renders_with_python_counts(tmp_path):
    res, manifest = run_themes(tmp_path, json.dumps(GOOD))
    assert res.status == "ok"
    assert [t["n_reports"] for t in res.themes] == [2, 1]
    assert (res.sample_size, res.sample_first, res.sample_last) == (2, "20260930", "20260930")
    assert manifest[-1]["file"] == "llm_response.json" and manifest[-1]["request_file"] == "llm_request.json"
    saved = json.loads((tmp_path / "raw/llm_request.json").read_text())
    assert saved["sample_report_keys"] == SAMPLE_KEYS


def test_valid_output_in_full_report(tmp_path, monkeypatch):
    monkeypatch.setenv(llm.KEY_ENV, "test-key-not-real")
    monkeypatch.chdir(tmp_path)
    call = fake_call(json.dumps(GOOD))
    code = cli.run("GZF", date(2021, 1, 1), tmp_path / "out", False, session=FakeSession(NARR),
                   sleep=lambda s: None, now=NOW, llm_call=call)
    assert code == 0
    report = (tmp_path / "out/report.md").read_text()
    assert "- **Lead migration** (2 reports). Narratives describe a lead that moved from its intended position.  " in report
    assert "  Report keys: 50949318, 50948697" in report
    assert "- **Skin irritation at site** (1 report)." in report
    assert "summarised by `claude-haiku-5-5`" in report
    assert "2 most recent reports with narrative text (`date_received` 2026-09-30 to 2026-09-30)" in report
    assert call.calls[0]["model"] == "claude-haiku-5-5"


OVERLAP = "A report can appear under more than one theme, so theme counts can add up to more than the sample size."


@pytest.mark.parametrize("themes", [GOOD, {"themes": GOOD["themes"][:1]}], ids=["two themes", "one theme"])
def test_overlap_note_directly_under_theme_list_whenever_themes_shown(tmp_path, monkeypatch, themes):
    monkeypatch.setenv(llm.KEY_ENV, "test-key-not-real")
    monkeypatch.chdir(tmp_path)
    cli.run("GZF", date(2021, 1, 1), tmp_path / "out", False, session=FakeSession(NARR),
            sleep=lambda s: None, now=NOW, llm_call=fake_call(json.dumps(themes)))
    lines = (tmp_path / "out/report.md").read_text().splitlines()
    last_keys = max(i for i, l in enumerate(lines) if l.startswith("  Report keys: "))
    assert lines[last_keys + 1:last_keys + 3] == ["", OVERLAP]
    assert lines.count(OVERLAP) == 1


@pytest.mark.parametrize("no_llm,text", [(True, json.dumps(GOOD)), (False, '{"themes": []}')], ids=["--no-llm", "fallback"])
def test_overlap_note_absent_when_no_themes_shown(tmp_path, monkeypatch, no_llm, text):
    monkeypatch.setenv(llm.KEY_ENV, "test-key-not-real")
    monkeypatch.chdir(tmp_path)
    cli.run("GZF", date(2021, 1, 1), tmp_path / "out", no_llm, session=FakeSession(NARR),
            sleep=lambda s: None, now=NOW, llm_call=fake_call(text))
    assert OVERLAP not in (tmp_path / "out/report.md").read_text()


def test_invalid_output_in_full_report_carries_on(tmp_path, monkeypatch):
    monkeypatch.setenv(llm.KEY_ENV, "test-key-not-real")
    monkeypatch.chdir(tmp_path)
    code = cli.run("GZF", date(2021, 1, 1), tmp_path / "out", False, session=FakeSession(NARR),
                   sleep=lambda s: None, now=NOW, llm_call=fake_call('{"themes": 5}'))
    report = (tmp_path / "out/report.md").read_text()
    assert code == 0
    assert "Themes unavailable: model output failed validation (failed rule: themes is a list of 1 to 5 objects)." in report
    assert "## 5. Limitations" in report


# --- what is sent ----------------------------------------------------------------------------

def test_sample_is_most_recent_with_text_and_sends_only_key_and_narrative(tmp_path):
    call = fake_call(json.dumps(GOOD))
    llm.generate_themes(NARR, sample_size=30, api_key="k", model="m", raw_dir=tmp_path, manifest=[], call=call)
    user = call.calls[0]["user"]
    payload = json.loads(user[user.index("["):])
    assert [p["mdr_report_key"] for p in payload] == SAMPLE_KEYS
    assert all(set(p) == {"mdr_report_key", "narrative"} for p in payload)


def test_sample_size_and_cap():
    recs = [{"mdr_report_key": str(i), "date_received": f"2026{(i % 9) + 1:02d}01", "mdr_text": [{"text": "t"}]} for i in range(80)]
    s = llm.select_sample(recs, 30)
    assert len(s) == 30
    assert [x["date_received"] for x in s] == sorted([x["date_received"] for x in s], reverse=True)
    assert len(llm.select_sample(recs, 50)) == 50
    with pytest.raises(ValueError):
        llm.select_sample(recs, 51)


def test_cli_rejects_sample_above_cap():
    with pytest.raises(SystemExit):
        cli.parse_args(["--product-code", "GZF", "--llm-sample", "51"])
    assert cli.parse_args(["--product-code", "GZF"]).llm_sample == 30


def test_system_prompt_rules():
    p = llm.SYSTEM_PROMPT
    assert "Describe only what the narratives say" in p
    assert "Make no causal claims" in p
    assert "Use no numbers" in p
    assert "Give no medical advice" in p


# --- key handling and fallbacks ---------------------------------------------------------------

def test_no_key_and_no_flag_skips_with_clear_line(tmp_path, monkeypatch):
    monkeypatch.delenv(llm.KEY_ENV, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-be-used")
    monkeypatch.chdir(tmp_path)
    call = fake_call(json.dumps(GOOD))
    code = cli.run("GZF", date(2021, 1, 1), tmp_path / "out", False, session=FakeSession(NARR),
                   sleep=lambda s: None, now=NOW, llm_call=call)
    assert code == 0
    assert call.calls == []
    assert "Themes skipped: MAUDE_ANTHROPIC_API_KEY is not set." in (tmp_path / "out/report.md").read_text()


def test_no_llm_needs_no_key_and_makes_no_call(tmp_path, monkeypatch):
    monkeypatch.delenv(llm.KEY_ENV, raising=False)
    call = fake_call(json.dumps(GOOD))
    cli.run("GZF", date(2021, 1, 1), tmp_path, True, session=FakeSession(NARR), sleep=lambda s: None, now=NOW, llm_call=call)
    assert call.calls == []
    assert "Themes not generated: run with --no-llm." in (tmp_path / "report.md").read_text()


def test_key_from_env_then_dotenv_only(tmp_path):
    assert llm.get_api_key({llm.KEY_ENV: "from-env"}, tmp_path / ".env") == "from-env"
    assert llm.get_api_key({"ANTHROPIC_API_KEY": "nope"}, tmp_path / ".env") is None
    (tmp_path / ".env").write_text('ANTHROPIC_API_KEY=nope\nexport MAUDE_ANTHROPIC_API_KEY="from-file"\n')
    assert llm.get_api_key({}, tmp_path / ".env") == "from-file"
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=nope\n")
    assert llm.get_api_key({}, tmp_path / ".env") is None


def test_missing_anthropic_package_gives_visible_line(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import anthropic -> ImportError
    res = llm.generate_themes(NARR, sample_size=30, api_key="k", model="m", raw_dir=tmp_path, manifest=[])
    assert res.status == "unavailable"
    assert res.line.startswith("Themes unavailable: the anthropic package is not installed")


def test_api_error_gives_visible_line(tmp_path):
    class FakeAPIError(Exception):
        status_code = 401

    def call(*a):
        raise FakeAPIError("bad key")

    manifest = []
    res = llm.generate_themes(NARR, sample_size=30, api_key="k", model="m", raw_dir=tmp_path, manifest=manifest, call=call)
    assert res.line == "Themes unavailable: Claude API call failed (FakeAPIError, HTTP 401)."
    assert manifest[-1]["error"] == "FakeAPIError, HTTP 401"


def test_refusal_gives_visible_line(tmp_path):
    def call(*a):
        return {"text": "", "stop_reason": "refusal", "model": "m", "usage": None, "request_id": None}

    res = llm.generate_themes(NARR, sample_size=30, api_key="k", model="m", raw_dir=tmp_path, manifest=[], call=call)
    assert res.line == "Themes unavailable: the model declined the request (stop_reason refusal)."


def test_no_narratives_gives_visible_line(tmp_path):
    recs = [dict(r, mdr_text=[]) for r in NARR]
    res = llm.generate_themes(recs, sample_size=30, api_key="k", model="m", raw_dir=tmp_path, manifest=[], call=fake_call("{}"))
    assert res.line == "Themes unavailable: no reports with narrative text in this window."
