"""rec2md batch: 폴더 일괄 전사. run_one·ffprobe를 바꿔 끼워 모델 없이 돈다."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rec2md import cli, cmd_batch

# 합성 파일 이름 → 길이(초)
LENGTHS = {
    "a_meeting.mp3": 1800.0,
    "b_call.M4A": 600.0,          # 확장자 대소문자 무시
    "c_empty.wav": 5.0,           # min-sec 미만
    "d_allday.mp3": 5 * 3600.0,   # 4시간 초과 → 분할 필요
    "e_broken.mp4": None,         # ffprobe 실패
}


@pytest.fixture
def folder(tmp_path, monkeypatch):
    d = tmp_path / "rec"
    d.mkdir()
    for name in LENGTHS:
        (d / name).write_bytes(b"")
    (d / "notes.txt").write_text("대상 아님")
    (d / "sub").mkdir()
    (d / "sub" / "nested.mp3").write_bytes(b"")  # 하위 폴더는 보지 않음

    def probe(path: Path) -> float:
        sec = LENGTHS.get(path.name)
        if sec is None:
            raise RuntimeError("ffprobe failed")
        return sec

    monkeypatch.setattr(cmd_batch.audio, "probe_duration", probe)
    return d


@pytest.fixture
def calls(monkeypatch):
    """run_one 대신 호출만 기록한다. FAIL에 든 이름은 실패시킨다."""
    seen: list[str] = []
    fail: set[str] = set()

    def fake(src: Path, args) -> Path:
        seen.append(src.name)
        assert Path(args.out).name == "_transcripts" or args.out
        if src.name in fail:
            raise RuntimeError("boom")
        return Path(args.out) / f"{src.stem}.raw.md"

    monkeypatch.setattr(cli, "run_one", fake)
    return seen, fail


def state_of(folder: Path) -> dict:
    return json.loads((folder / "_transcripts" / cmd_batch.STATE_FILE).read_text(encoding="utf-8"))


def test_dry_run_reports_and_does_nothing(folder, calls, capsys):
    cmd_batch.main([str(folder), "--dry-run"])
    out = capsys.readouterr().out
    seen, _ = calls
    assert seen == []
    assert not (folder / "_transcripts" / cmd_batch.STATE_FILE).exists()
    rows = {line.split()[0]: line for line in out.splitlines() if line.split() and line.split()[0] in LENGTHS}
    assert set(rows) == set(LENGTHS)  # notes.txt, sub/nested.mp3는 없음
    assert rows["a_meeting.mp3"].endswith(cmd_batch.TODO) and "00:30:00" in rows["a_meeting.mp3"]
    assert rows["c_empty.wav"].endswith(cmd_batch.SHORT)
    assert rows["d_allday.mp3"].endswith(cmd_batch.SPLIT)
    assert rows["e_broken.mp4"].endswith(cmd_batch.BROKEN)
    # 처리 대상 2개(30분+10분), local 12배 가정 → 약 3분 20초
    assert "처리 대상 2개 · 총 00:40:00 · local 예상 처리시간 약 00:03:20" in out
    assert "분할이 필요해" in out


def test_run_skips_and_records_state(folder, calls):
    seen, _ = calls
    cmd_batch.main([str(folder)])
    assert seen == ["a_meeting.mp3", "b_call.M4A"]
    st = state_of(folder)
    assert st == {"a_meeting.mp3": {"status": "done", "sec": 1800.0},
                  "b_call.M4A": {"status": "done", "sec": 600.0}}
    # 다시 돌리면 done은 건너뛴다
    seen.clear()
    cmd_batch.main([str(folder)])
    assert seen == []


def test_failure_continues_and_resume_retries(folder, calls):
    seen, fail = calls
    fail.add("a_meeting.mp3")
    with pytest.raises(SystemExit, match="실패 1개"):
        cmd_batch.main([str(folder)])
    assert seen == ["a_meeting.mp3", "b_call.M4A"]  # 실패 뒤에도 다음 파일로
    st = state_of(folder)
    assert st["a_meeting.mp3"]["status"] == "failed" and "boom" in st["a_meeting.mp3"]["error"]
    assert st["b_call.M4A"]["status"] == "done"

    # 고쳐서 다시: 실패한 것만 다시 시도
    seen.clear()
    fail.clear()
    cmd_batch.main([str(folder)])
    assert seen == ["a_meeting.mp3"]
    assert state_of(folder)["a_meeting.mp3"] == {"status": "done", "sec": 1800.0}


def test_dry_run_shows_retry_and_done(folder, calls, capsys):
    out_dir = folder / "_transcripts"
    cmd_batch.save_state(out_dir / cmd_batch.STATE_FILE, {
        "a_meeting.mp3": {"status": "done", "sec": 1800.0},
        "b_call.M4A": {"status": "failed", "sec": 600.0, "error": "x"}})
    cmd_batch.main([str(folder), "--dry-run"])
    out = capsys.readouterr().out
    assert any(line.startswith("a_meeting.mp3") and line.endswith(cmd_batch.DONE) for line in out.splitlines())
    assert any(line.startswith("b_call.M4A") and line.endswith(cmd_batch.RETRY) for line in out.splitlines())
    assert "처리 대상 1개 · 총 00:10:00" in out


def test_options_pass_through_and_out_dir(folder, calls, tmp_path, monkeypatch):
    got = {}

    def fake(src, args):
        got.update(out=args.out, speakers=args.speakers, min_sec=args.min_sec)
        return Path(args.out)

    monkeypatch.setattr(cli, "run_one", fake)
    out = tmp_path / "elsewhere"
    cmd_batch.main([str(folder), "--out", str(out), "--speakers", "3", "--min-sec", "700"])
    assert got == {"out": str(out), "speakers": 3, "min_sec": 700.0}
    assert (out / cmd_batch.STATE_FILE).is_file()
    assert set(json.loads((out / cmd_batch.STATE_FILE).read_text())) == {"a_meeting.mp3"}  # b는 700초 미만


def test_cli_routes_batch(folder, calls, capsys):
    cli.main(["batch", str(folder), "--dry-run"])
    assert "처리 대상 2개" in capsys.readouterr().out


def test_missing_or_empty_folder(tmp_path):
    with pytest.raises(SystemExit, match="폴더 없음"):
        cmd_batch.main([str(tmp_path / "nope")])
    (tmp_path / "empty").mkdir()
    with pytest.raises(SystemExit, match="처리할 녹음"):
        cmd_batch.main([str(tmp_path / "empty")])
