"""rec2md batch: 폴더 일괄 전사. run_one·ffprobe를 바꿔 끼워 모델 없이 돈다."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rec2md import cli, cmd_batch, trim

# 합성 파일 이름 → 길이(초)
LENGTHS = {
    "a_meeting.mp3": 1800.0,
    "b_call.M4A": 600.0,          # 확장자 대소문자 무시
    "c_empty.wav": 5.0,           # min-sec 미만
    "d_allday.mp3": 7 * 3600.0,   # 6시간 초과 → 분할 필요
    "e_broken.mp4": None,         # ffprobe 실패
}


# 긴 무음을 뺀 추정 말소리 길이 (없으면 파일 길이와 같음)
SPEECH: dict[str, float] = {}
GAPS: list[float] = []  # speech_sec에 넘어온 min_gap


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
    # 말소리 추정(소리 크기)은 기본으로 파일 길이 그대로, SPEECH에 있으면 그 값
    GAPS.clear()

    def speech(path, min_gap=trim.MIN_GAP):
        GAPS.append(min_gap)
        return SPEECH.get(path.name, LENGTHS.get(path.name))
    monkeypatch.setattr(cmd_batch, "speech_sec", speech)
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
    assert "처리 대상 2개 · 말소리 총 00:40:00 · local 예상 처리시간 약 00:03:20" in out
    assert "분할이 필요해" in out


def test_run_skips_and_records_state(folder, calls):
    seen, _ = calls
    cmd_batch.main([str(folder)])
    assert seen == ["a_meeting.mp3", "b_call.M4A"]
    st = state_of(folder)
    assert st == {"a_meeting.mp3": {"status": "done", "sec": 1800.0, "source": str((folder / "a_meeting.mp3").resolve())},
                  "b_call.M4A": {"status": "done", "sec": 600.0, "source": str((folder / "b_call.M4A").resolve())}}
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
    assert state_of(folder)["a_meeting.mp3"]["status"] == "done"


def test_dry_run_shows_retry_and_done(folder, calls, capsys):
    out_dir = folder / "_transcripts"
    cmd_batch.save_state(out_dir / cmd_batch.STATE_FILE, {
        "a_meeting.mp3": {"status": "done", "sec": 1800.0},
        "b_call.M4A": {"status": "failed", "sec": 600.0, "error": "x"}})
    cmd_batch.main([str(folder), "--dry-run"])
    out = capsys.readouterr().out
    assert any(line.startswith("a_meeting.mp3") and line.endswith(cmd_batch.DONE) for line in out.splitlines())
    assert any(line.startswith("b_call.M4A") and line.endswith(cmd_batch.RETRY) for line in out.splitlines())
    assert "처리 대상 1개 · 말소리 총 00:10:00" in out


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


def test_same_stem_is_not_processed(folder, calls, capsys):
    """회의.mp3와 회의.wav는 출력(회의.segments.json)이 겹친다 → 둘 다 처리하지 않고 알린다 (Codex 리뷰 #28)."""
    (folder / "A_MEETING.wav").write_bytes(b"")  # 대소문자만 달라도 겹친다
    LENGTHS["A_MEETING.wav"] = 900.0
    try:
        seen, _ = calls
        with pytest.raises(SystemExit, match="출력 이름이 겹쳐"):
            cmd_batch.main([str(folder)])
        assert seen == ["b_call.M4A"]
        out = capsys.readouterr().out
        assert sum(1 for line in out.splitlines() if line.endswith(cmd_batch.CLASH)) == 2
        assert "a_meeting.mp3" not in state_of(folder)
    finally:
        del LENGTHS["A_MEETING.wav"]


def test_other_folder_with_same_name_is_not_skipped_as_done(folder, calls, tmp_path):
    """같은 --out을 다른 폴더가 이미 썼으면, 같은 이름 파일을 '이미 처리'로 건너뛰지 않고 겹침으로 막는다."""
    seen, _ = calls
    out = tmp_path / "shared"
    cmd_batch.main([str(folder), "--out", str(out)])
    other = tmp_path / "other"
    other.mkdir()
    (other / "a_meeting.mp3").write_bytes(b"")
    seen.clear()
    LENGTHS["a_meeting.mp3"], old = 1234.0, LENGTHS["a_meeting.mp3"]  # 이름만 같고 다른 녹음
    try:
        with pytest.raises(SystemExit, match="출력 이름이 겹쳐"):
            cmd_batch.main([str(other), "--out", str(out)])
    finally:
        LENGTHS["a_meeting.mp3"] = old
    assert seen == []  # 덮어쓰지도, 건너뛰지도(이미 처리) 않는다


def test_state_without_source_is_still_resumed(folder, calls):
    """source가 없는 예전 상태 파일도 같은 원본으로 보고 이어서 돈다."""
    seen, _ = calls
    cmd_batch.save_state(folder / "_transcripts" / cmd_batch.STATE_FILE,
                         {"a_meeting.mp3": {"status": "done", "sec": 1800.0}})
    cmd_batch.main([str(folder)])
    assert seen == ["b_call.M4A"]


def test_same_stem_from_previous_batch_is_a_clash(folder, calls, tmp_path):
    """앞 배치의 a/meeting.mp3와 이번 배치의 b/meeting.wav는 출력이 같다 → 겹침 (Codex 재리뷰 #28)."""
    seen, _ = calls
    out = tmp_path / "shared"
    cmd_batch.main([str(folder), "--out", str(out)])
    other = tmp_path / "other"
    other.mkdir()
    (other / "a_meeting.wav").write_bytes(b"")
    LENGTHS["a_meeting.wav"] = 900.0
    try:
        seen.clear()
        with pytest.raises(SystemExit, match="출력 이름이 겹쳐"):
            cmd_batch.main([str(other), "--out", str(out)])
        assert seen == []
    finally:
        del LENGTHS["a_meeting.wav"]


def test_moved_source_folder_still_resumes(folder, calls, tmp_path):
    """원본 폴더를 옮기거나 다른 경로로 붙어도(같은 파일명·같은 길이) 이어서 돈다 (리뷰 #28 3차)."""
    seen, _ = calls
    out = tmp_path / "out"
    cmd_batch.main([str(folder), "--out", str(out)])
    moved = tmp_path / "moved"
    folder.rename(moved)
    seen.clear()
    cmd_batch.main([str(moved), "--out", str(out)])
    assert seen == []  # done은 그대로 건너뜀
    # 같은 이름이지만 길이가 다른 파일은 다른 원본 → 겹침
    LENGTHS["a_meeting.mp3"], old = 999.0, LENGTHS["a_meeting.mp3"]
    try:
        with pytest.raises(SystemExit, match="상태 파일"):
            cmd_batch.main([str(moved), "--out", str(out)])
    finally:
        LENGTHS["a_meeting.mp3"] = old


def test_long_recording_with_mostly_silence_is_processed(folder, calls, capsys):
    """8시간 녹음이라도 녹음기를 안 꺼서 생긴 무음을 빼면 2시간이면 처리 대상이다. 분할 판단은 말소리 길이로."""
    (folder / "f_forgot.mp3").write_bytes(b"")
    LENGTHS["f_forgot.mp3"] = 8 * 3600.0
    SPEECH["f_forgot.mp3"] = 2 * 3600.0
    try:
        cmd_batch.main([str(folder), "--dry-run"])
        out = capsys.readouterr().out
        row = next(line for line in out.splitlines() if line.startswith("f_forgot.mp3"))
        assert "08:00:00" in row and "02:00:00" in row and row.endswith(cmd_batch.TODO)
        assert any(line.startswith("d_allday.mp3") and line.endswith(cmd_batch.SPLIT) for line in out.splitlines())
        assert "말소리 총 02:40:00" in out and "긴 무음 06:00:00는 전사 전에 잘라냄" in out
        # --no-trim이면 무음을 자르지 않으니 추정하지 않고 파일 길이로 판단 → 분할 필요
        cmd_batch.main([str(folder), "--dry-run", "--no-trim"])
        row = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("f_forgot.mp3"))
        assert row.endswith(cmd_batch.SPLIT)
        seen, _ = calls
        cmd_batch.main([str(folder)])
        assert "f_forgot.mp3" in seen
    finally:
        del LENGTHS["f_forgot.mp3"], SPEECH["f_forgot.mp3"]


def test_dense_recording_up_to_max_is_processed(folder, calls, capsys):
    """무음이 거의 없는 5시간 회의도 한 번에 처리한다(한도 6시간, 근거는 docs/PLAN.md 실측)."""
    (folder / "g_dense.mp3").write_bytes(b"")
    LENGTHS["g_dense.mp3"] = SPEECH["g_dense.mp3"] = 5 * 3600.0
    try:
        cmd_batch.main([str(folder), "--dry-run"])
        row = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("g_dense.mp3"))
        assert row.endswith(cmd_batch.TODO)
    finally:
        del LENGTHS["g_dense.mp3"], SPEECH["g_dense.mp3"]


def test_trim_min_gap_reaches_speech_estimate(folder, capsys):
    cmd_batch.main([str(folder), "--dry-run", "--trim-min-gap", "2"])
    assert GAPS and set(GAPS) == {120.0}
