import json
import re
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FEATURE = ROOT / "specs/001-initial-live-recorder"


def _section(text: str, start: str, end: str) -> str:
    return text.split("\n## " + start, 1)[1].split("\n## " + end, 1)[0]


def _fixed_ids(source: str, tag: str) -> list[str]:
    return re.findall(rf"^- \*\*({tag}-\d{{3}})\*\* ", source, re.MULTILINE)


def _check_ids(source: list[str], trace: list[str]) -> None:
    assert len(set(source)) == len(source), "原本IDの重複"
    assert len(set(trace)) == len(trace), "追跡表IDの重複"
    assert trace == source, "追跡表に欠落・不明なIDまたは順序の不一致"


def test_traceability_covers_original_sections_ids_ui_and_risks():
    requirements = (ROOT / "requirements.md").read_text()
    ui = (ROOT / "docs/UI_UX.md").read_text()
    design = (ROOT / "docs/design/07-decisions-and-gaps.md").read_text()
    trace = (FEATURE / "traceability.md").read_text()
    tasks = (FEATURE / "tasks.md").read_text()
    spec = (FEATURE / "spec.md").read_text()
    ac_ids = _fixed_ids(_section(requirements, "23.", "24."), "AC")
    tc_ids = _fixed_ids(_section(requirements, "17.", "18."), "TC")
    _check_ids(ac_ids, re.findall(r"^\| §[^|\n]+ \| (AC-\d{3}) \|", trace, re.M))
    _check_ids(tc_ids, re.findall(r"^\| (TC-\d{3}) \|", trace, re.M))
    _check_ids(
        re.findall(r"^## (\d+)\.", requirements, re.M),
        re.findall(r"^\| §(\d+) \|", _section(trace, "原本の章と実装への導線", "UI受け入れ項目"), re.M),
    )
    assert len(re.findall(r"^\| §16\.\d+ 項目\d+ \|", trace, re.M)) == len(
        re.findall(r"^- \[ \] ", _section(ui, "16.", "17."), re.M)
    )
    _check_ids(re.findall(r"^\| (RISK-\d{2}) \|", design, re.M),
               re.findall(r"^\| (RISK-\d{2}) \|", trace, re.M))
    for row in trace.splitlines():
        if not row.startswith("|") or not re.search(r"\b(?:AC|TC)-\d{3}\b", row):
            continue
        assert not set(re.findall(r"\bTC-\d{3}\b", row)) - set(tc_ids)
        assert not set(re.findall(r"\bAC-\d{3}\b", row)) - set(ac_ids)
        assert set(re.findall(r"\bFR-\d{3}\b", row)) <= set(re.findall(r"\bFR-\d{3}\b", spec))
        assert set(re.findall(r"\bT\d{3}\b", row)) <= set(re.findall(r"\bT\d{3}\b", tasks))
        assert re.search("未検証|ローカル検証済み|実機検証済み|実装不可", row)
        if "ローカル検証済み" in row:
            for name in re.findall(r"tests/[a-z_]+\.py", row):
                assert (ROOT / name).is_file(), row


@pytest.mark.parametrize("injected", [["TC-001"], ["TC-001", "TC-001"], ["TC-001", "TC-099"]])
def test_trace_rejects_missing_duplicate_and_wrong_test_id(injected):
    with pytest.raises(AssertionError):
        _check_ids(["TC-001", "TC-002"], injected)


def test_feature_preflight_rejects_mismatch_without_changes(tmp_path):
    check_feature = runpy.run_path(str(ROOT / "scripts/check_feature.py"))["check_feature"]
    config = tmp_path / ".specify/feature.json"
    config.parent.mkdir()
    setting = {"feature_directory": "specs/001-initial-live-recorder"}
    config.write_text(json.dumps(setting))
    spec = tmp_path / setting["feature_directory"] / "spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("既存の仕様")
    assert check_feature(tmp_path, {}) == spec.parent
    for env in (
        {"SPECIFY_FEATURE_DIRECTORY": "specs/002-other"},
        {"SPECIFY_FEATURE": "002-other"},
    ):
        with pytest.raises(ValueError, match="一致しません"):
            check_feature(tmp_path, env)
    other_directory = tmp_path / "specs/002-initial-live-recorder"
    other_directory.mkdir()
    with pytest.raises(ValueError, match="候補"):
        check_feature(tmp_path, {})
    assert spec.read_text() == "既存の仕様"
    assert json.loads(config.read_text()) == setting
