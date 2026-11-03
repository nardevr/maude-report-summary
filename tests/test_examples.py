"""The committed sample report must carry the same Limitations text as the code that renders it."""

from pathlib import Path

from maude_summary.render import LIMITATIONS_MD

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "GZF" / "report.md"
HEADING = "## 5. Limitations\n\n"
END = "\n\n---\n"


def test_sample_section_5_matches_limitations_md():
    text = SAMPLE.read_text()
    assert text.count(HEADING) == 1
    body = text.split(HEADING, 1)[1]
    assert END in body
    assert body.split(END, 1)[0] == LIMITATIONS_MD
