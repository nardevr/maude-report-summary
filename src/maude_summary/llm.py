"""Narrative themes via the Claude API, with strict validation of the model output (spec v1.1 rule 2).

The model only returns theme labels, one-sentence descriptions and the report keys it used.
Python computes every number, including each theme's report count (unique keys).

The Anthropic SDK is imported lazily inside ``call_claude`` so the package and its tests
run without ``anthropic`` installed. The key is read only from MAUDE_ANTHROPIC_API_KEY
(environment, then a git-ignored ``.env`` file), never from ANTHROPIC_API_KEY, and is
passed to the SDK explicitly so the SDK does not look for credentials elsewhere.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

KEY_ENV = "MAUDE_ANTHROPIC_API_KEY"
MODEL_ENV = "MAUDE_LLM_MODEL"
DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_SAMPLE = 30
MAX_SAMPLE = 50
MAX_THEMES = 5
MAX_TOKENS = 16000
API_URL = "https://api.anthropic.com/v1/messages"

SYSTEM_PROMPT = """\
You read narratives from U.S. FDA MAUDE medical device adverse event reports and group them into themes.

Rules:
- Describe only what the narratives say. Make no causal claims: do not say or imply that a device \
caused, led to or resulted in any outcome.
- Use no numbers. Labels and descriptions must not contain any digit: no counts, percentages, dates, \
ages, durations, model numbers or lot numbers.
- Give no medical advice, and do not judge whether a device or manufacturer is safe or unsafe.
- Return between one and five themes. Each theme has a short label (at most sixty characters), a \
one-sentence description (at most two hundred characters), and report_keys: the mdr_report_key \
values, copied exactly from the input, of the reports whose narratives show that theme.
- Use only report keys that appear in the input.

Return only a JSON object of the form {"themes": [{"label": ..., "description": ..., "report_keys": [...]}]}."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "themes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "description": {"type": "string"},
                    "report_keys": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["label", "description", "report_keys"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["themes"],
    "additionalProperties": False,
}

# Rule names shown in the fallback line (spec v1.1, section 4, rule 2).
RULE_JSON = "parses as JSON"
RULE_TOP = "top level is an object with exactly one key, themes"
RULE_COUNT = "themes is a list of 1 to 5 objects"
RULE_KEYS = "each theme has exactly the keys label, description and report_keys"
RULE_LABEL = "label is a string of 1 to 60 characters"
RULE_DESC = "description is a string of 1 to 200 characters"
RULE_RKEYS = "report_keys is a non-empty list of unique strings"
RULE_IN_SAMPLE = "every report key is in the sample sent"
RULE_DIGITS = "no digit in any label or description"

NO_LLM_LINE = "Themes not generated: run with --no-llm."
NO_KEY_LINE = f"Themes skipped: {KEY_ENV} is not set."


@dataclass
class ThemesResult:
    status: str  # "ok" | "skipped" | "unavailable"
    line: str = ""
    themes: list[dict] = field(default_factory=list)  # validated themes, each with "n_reports" added by Python
    model: str | None = None
    sample_size: int = 0
    sample_first: str | None = None  # earliest date_received in the sample (YYYYMMDD)
    sample_last: str | None = None


def get_api_key(environ=None, dotenv_path: Path | str = ".env") -> str | None:
    """MAUDE_ANTHROPIC_API_KEY from the environment, else from a .env file. Nothing else is consulted."""
    environ = os.environ if environ is None else environ
    value = (environ.get(KEY_ENV) or "").strip()
    if value:
        return value
    path = Path(dotenv_path)
    if not path.is_file():
        return None
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        name, sep, val = line.partition("=")
        if sep and name.strip() == KEY_ENV:
            val = val.strip().strip("'\"")
            return val or None
    return None


def narrative_text(record: dict) -> str:
    parts = [str(t.get("text") or "").strip() for t in record.get("mdr_text") or []]
    return "\n\n".join(p for p in parts if p)


def select_sample(records: list[dict], n: int) -> list[dict]:
    """The n most recent reports (date_received, then mdr_report_key, descending) that have narrative text.

    Returns [{"mdr_report_key", "narrative", "date_received"}]; only key and narrative are sent.
    """
    if not 1 <= n <= MAX_SAMPLE:
        raise ValueError(f"sample size must be between 1 and {MAX_SAMPLE}")
    with_text = [r for r in records if narrative_text(r)]
    with_text.sort(key=lambda r: (str(r.get("date_received", "")), str(r["mdr_report_key"])), reverse=True)
    return [{"mdr_report_key": str(r["mdr_report_key"]), "narrative": narrative_text(r),
             "date_received": str(r.get("date_received", ""))} for r in with_text[:n]]


def build_user_message(sample: list[dict]) -> str:
    payload = [{"mdr_report_key": s["mdr_report_key"], "narrative": s["narrative"]} for s in sample]
    return ("Here are the report narratives as a JSON list. Group them into themes following the rules.\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=1))


class ValidationError(Exception):
    def __init__(self, rule: str):
        super().__init__(rule)
        self.rule = rule


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key in JSON object")
    return dict(pairs)


def validate_output(text: str, sample_keys: set[str]) -> list[dict]:
    """Apply every check in spec rule 2. Returns the themes, or raises ValidationError(rule)."""
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except (ValueError, TypeError):
        raise ValidationError(RULE_JSON)
    if not isinstance(data, dict) or set(data) != {"themes"}:
        raise ValidationError(RULE_TOP)
    themes = data["themes"]
    if not isinstance(themes, list) or not 1 <= len(themes) <= MAX_THEMES or not all(isinstance(t, dict) for t in themes):
        raise ValidationError(RULE_COUNT)
    for t in themes:
        if set(t) != {"label", "description", "report_keys"}:
            raise ValidationError(RULE_KEYS)
        if not isinstance(t["label"], str) or not 1 <= len(t["label"]) <= 60:
            raise ValidationError(RULE_LABEL)
        if not isinstance(t["description"], str) or not 1 <= len(t["description"]) <= 200:
            raise ValidationError(RULE_DESC)
        keys = t["report_keys"]
        if (not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys)
                or len(set(keys)) != len(keys)):
            raise ValidationError(RULE_RKEYS)
        if not set(keys) <= sample_keys:
            raise ValidationError(RULE_IN_SAMPLE)
    for t in themes:
        if any(c.isdigit() for c in t["label"] + t["description"]):
            raise ValidationError(RULE_DIGITS)
    return themes


def call_claude(system: str, user: str, model: str, api_key: str) -> dict:
    """One Messages API call. Returns {"text", "stop_reason", "model", "usage", "request_id"}."""
    import anthropic  # lazy: tests and --no-llm runs never need the package

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    usage = response.usage.to_dict() if hasattr(response.usage, "to_dict") else None
    return {"text": text, "stop_reason": response.stop_reason, "model": response.model,
            "usage": usage, "request_id": getattr(response, "_request_id", None)}


def _write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def generate_themes(records: list[dict], *, sample_size: int, api_key: str, model: str, raw_dir: Path,
                    manifest: list[dict], call: Callable[..., dict] | None = None,
                    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> ThemesResult:
    """Select the sample, call the model once, validate, and return themes or a visible fallback line."""
    sample = select_sample(records, sample_size)
    if not sample:
        return ThemesResult("unavailable", "Themes unavailable: no reports with narrative text in this window.")
    res = ThemesResult("unavailable", model=model, sample_size=len(sample),
                       sample_first=min(s["date_received"] for s in sample),
                       sample_last=max(s["date_received"] for s in sample))
    user = build_user_message(sample)
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    _write_json(raw_dir / "llm_request.json", {"model": model, "max_tokens": MAX_TOKENS, "system": SYSTEM_PROMPT,
                                               "user": user, "output_schema": OUTPUT_SCHEMA,
                                               "sample_report_keys": [s["mdr_report_key"] for s in sample]})
    fetched = now().isoformat(timespec="seconds")
    if call is None:
        try:
            import anthropic  # noqa: F401
        except ImportError:
            res.line = "Themes unavailable: the anthropic package is not installed (pip install -r requirements.txt)."
            return res
        call = call_claude
    try:
        out = call(SYSTEM_PROMPT, user, model, api_key)
    except Exception as exc:  # any SDK or network failure: visible fallback, never silent
        status = getattr(exc, "status_code", None)
        detail = f"{type(exc).__name__}" + (f", HTTP {status}" if status else "")
        manifest.append({"file": None, "url": API_URL, "fetched_utc": fetched, "http_status": status,
                         "error": detail, "kind": "claude_messages"})
        res.line = f"Themes unavailable: Claude API call failed ({detail})."
        return res

    _write_json(raw_dir / "llm_response.json", {"fetched_utc": fetched, **out})
    manifest.append({"file": "llm_response.json", "url": API_URL, "fetched_utc": fetched, "http_status": 200,
                     "kind": "claude_messages", "model": out.get("model"), "request_id": out.get("request_id"),
                     "request_file": "llm_request.json"})
    if out.get("stop_reason") == "refusal":
        res.line = "Themes unavailable: the model declined the request (stop_reason refusal)."
        return res
    try:
        themes = validate_output(out.get("text", ""), {s["mdr_report_key"] for s in sample})
    except ValidationError as err:
        res.line = f"Themes unavailable: model output failed validation (failed rule: {err.rule})."
        return res
    res.status = "ok"
    res.themes = [dict(t, n_reports=len(set(t["report_keys"]))) for t in themes]
    return res
