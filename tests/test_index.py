"""rec2md index: 백로그 색인·이름 바꾸기. 합성 segments.json과 가짜 미디어만 쓴다."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rec2md import index as ix
from rec2md.cmd_index import main as index_main
from rec2md.segments import Segment, Transcript


def write_seg(out: Path, stem: str, source: str, texts=("예산 검토를 합시다",), duration=1800.0, meta=None):
    segs = [Segment(i * 10.0, i * 10.0 + 8, t, speaker=f"화자{i % 2 + 1}") for i, t in enumerate(texts)]
    t = Transcript(source, duration, "local", "ko", segs, meta=meta or {})
    t.to_json(out / f"{stem}.segments.json")
    (out / f"{stem}.raw.md").write_text("raw", encoding="utf-8")
    (out / f"{stem}.녹취록.md").write_text("정리", encoding="utf-8")


@pytest.fixture
def rec(tmp_path):
    """녹음 폴더(rec)와 출력 폴더(rec/_transcripts). 이름은 전부 가짜."""
    media = tmp_path / "rec"
    out = media / "_transcripts"
    out.mkdir(parents=True)
    state = {}
    for name in ("REC00010.MP3", "REC00012.MP3", "20990105_가상회의-REC00011.MP3"):
        (media / name).write_bytes(name.encode() * 100)
        state[name] = {"status": "done", "sec": 1800.0, "source": str(media / name)}
    (out / ".rec2md-state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    write_seg(out, "REC00010", "REC00010.MP3",
              texts=("오늘은 3월 4일 화요일입니다", "다음 주에 2차 회의를 합니다", "2099년 예산"),
              meta={"trim": {"kept_sec": 1500.0},
                    "meeting_bounds": {"start": 0, "end": 1700, "confidence": "high", "evidence": []}})
    write_seg(out, "REC00012", "REC00012.MP3")
    write_seg(out, "20990105_가상회의-REC00011", "20990105_가상회의-REC00011.MP3")
    return media, out


# ── 이름 형식 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("date,prec,want", [
    ("2099-03-04", "day", "20990304"),
    ("2099-03-04", "month", "209903xx"),
    ("2099-03", "day", "209903xx"),        # 날짜가 정밀도보다 짧으면 내린다
    ("2099", "year", "2099xxxx"),
    (None, "unknown", "xxxxxxxx"),
    ("2099-03-04", None, "xxxxxxxx"),
])
def test_date_token(date, prec, want):
    assert ix.date_token(date, prec) == want


def test_format_name_is_windows_safe_and_keeps_rec():
    name = ix.format_name("2099-03-04", "day", '가상: 회의? "기획" <1>.', "REC00010")
    assert name == "20990304_가상_회의_기획_1-REC00010"
    assert not any(c in name for c in '\\/:*?"<>|')
    # 주제에 이미 REC 번호가 있으면 다시 붙이지 않는다
    assert ix.format_name("2099", "year", "메모 REC00010", "REC00010").endswith("메모_REC00010")
    # 너무 긴 주제는 바이트 한도로 자른다
    assert len(ix.format_name("2099-03-04", "day", "가" * 200, "REC00010").encode()) <= ix.MAX_STEM_BYTES


# ── build ────────────────────────────────────────────────────────────────

def test_build_mechanical_clues_neighbors(rec):
    media, out = rec
    idx = ix.build(out)
    by = {i["source"]: i for i in idx["items"]}
    a = by["REC00010.MP3"]
    assert a["rec"] == "REC00010" and a["speech_sec"] == 1500.0 and a["bounds"]["end"] == 1700
    assert a["speakers"] == 2 and a["fingerprint"]
    kinds = {c["kind"] for c in a["clues"]}
    assert {"date", "weekday", "relative", "round", "year"} <= kinds
    assert not any(c["kind"] == "month" for c in a["clues"])  # "3월 4일"의 3월은 month로 따로 잡지 않는다
    # 사람이 붙인 이름의 날짜가 앵커 → REC00012의 앞 앵커는 REC00011
    assert by["20990105_가상회의-REC00011.MP3"]["name_date"] == "2099-01-05"
    assert by["REC00012.MP3"]["neighbors"]["prev"] == {"rec": "REC00011", "date": "2099-01-05"}
    assert by["REC00010.MP3"]["neighbors"]["next"]["rec"] == "REC00011"
    md = (out / "index.md").read_text(encoding="utf-8") if (out / "index.md").exists() else ix.render_md(idx)
    assert "화요일" not in md  # 단서 원문은 md에 넣지 않는다


def test_rebuild_keeps_agent_and_marks_stale(rec):
    media, out = rec
    idx = ix.build(out)
    item = ix.find_item(idx, "REC00012")
    ix.set_agent(item, date="2099-01-06", date_precision="day", date_confidence="high", topic="가상 점검")
    item["approved"] = True
    ix.save_json(out / ix.INDEX_JSON, idx)
    (media / "REC00012.MP3").write_bytes(b"other recording")  # 녹음기 번호 재사용으로 다른 녹음이 됨
    idx = ix.build(out)
    item = ix.find_item(idx, "REC00012")
    assert item["agent"]["topic"] == "가상 점검" and item["approved"]
    assert item["stale"]


def test_set_downgrades_day_without_high_confidence(rec):
    _, out = rec
    idx = ix.build(out)
    item = ix.find_item(idx, "REC00012")
    ix.set_agent(item, date="2099-01-06", date_precision="day", date_confidence="medium", topic="가상")
    assert item["agent"]["date_precision"] == "month"
    assert ix.proposed(item) == "209901xx_가상-REC00012"


def test_rec_dup_flag(rec):
    media, out = rec
    write_seg(out, "20990110_다른회의-REC00012", "20990110_다른회의-REC00012.MP3")
    idx = ix.build(out)
    assert ix.find_item(idx, "REC00012")["rec_dup"]


# ── rename ───────────────────────────────────────────────────────────────

def _approve(out, key, **agent):
    idx = ix.load_index(out)
    item = ix.find_item(idx, key)
    ix.set_agent(item, **agent)
    item["approved"] = True
    ix.save_json(out / ix.INDEX_JSON, idx)


def test_rename_dry_run_then_apply_and_undo(rec, capsys):
    media, out = rec
    index_main([str(out)])
    _approve(out, "REC00010", date="2099-03-04", date_precision="day", date_confidence="high", topic="가상 기획회의")
    before = sorted(p.name for p in media.iterdir())

    index_main(["rename", str(out)])
    assert "바꿀 예정" in capsys.readouterr().out
    assert (out / "REC00010.segments.json").exists()  # dry-run은 아무것도 안 바꿈

    index_main(["rename", str(out), "--apply"])
    new = "20990304_가상_기획회의-REC00010"
    assert {p.name for p in out.glob(f"{new}.*")} == {f"{new}.segments.json", f"{new}.raw.md", f"{new}.녹취록.md"}
    assert not list(out.glob("REC00010.*"))
    assert sorted(p.name for p in media.iterdir()) == before  # 녹음 원본 폴더는 그대로
    state = json.loads((out / ".rec2md-state.json").read_text(encoding="utf-8"))
    assert "REC00010.MP3" in state and state["REC00010.MP3"]["outputs"] == new  # batch가 다시 전사하지 않게 키는 유지
    # 다시 색인해도 바뀐 이름의 항목에 판단이 남는다
    idx = ix.build(out)
    assert ix.find_item(idx, "REC00010.MP3")["stem"] == new
    assert ix.find_item(idx, "REC00010.MP3")["agent"]["topic"] == "가상 기획회의"

    index_main(["rename", str(out), "--undo"])
    assert (out / "REC00010.segments.json").exists() and not list(out.glob(f"{new}.*"))


def test_rename_skips_clash_and_forbidden(rec, capsys):
    _, out = rec
    index_main([str(out)])
    _approve(out, "REC00010", date="2099", date_precision="year", date_confidence="low", topic="같은 이름")
    _approve(out, "REC00012", date="2099", date_precision="year", date_confidence="low", topic="같은 이름")
    (out / "x.txt").write_text("")
    plans = ix.plan_renames(out, ix.load_index(out))
    skips = [p["skip"] for p in plans]
    assert skips.count(None) == 2  # REC 번호가 달라 이름이 갈린다
    _approve(out, "REC00012", proposed_name="2099xxxx_같은_이름-REC00010")  # 직접 지정한 이름이 겹침
    plans = ix.plan_renames(out, ix.load_index(out))
    assert any(p["skip"] and "겹침" in p["skip"] for p in plans)
    _approve(out, "REC00012", proposed_name="2099xxxx_물음표?")
    plans = ix.plan_renames(out, ix.load_index(out))
    assert any(p["skip"] and "쓸 수 없는" in p["skip"] for p in plans)


def test_rename_skips_existing_target(rec):
    _, out = rec
    ix.build(out)
    _approve(out, "REC00012", proposed_name="20990105_가상회의-REC00011")  # 다른 항목의 출력과 같은 이름
    plans = ix.plan_renames(out, ix.load_index(out))
    assert plans[0]["skip"].startswith("이미 있는 파일")


def test_copy_media_never_next_to_source(rec, tmp_path):
    media, out = rec
    ix.build(out)
    _approve(out, "REC00010", date="2099-03-04", date_precision="day", date_confidence="high", topic="가상")
    idx = ix.load_index(out)
    plans = ix.plan_renames(out, idx)
    ix.apply_renames(out, idx, plans, copy_media=media)
    assert "원본 폴더와 같음" in plans[0]["skip"]
    assert (out / "REC00010.segments.json").exists()  # 사본을 못 만들면 출력 이름도 그대로

    copies = tmp_path / "copies"
    idx = ix.load_index(out)
    plans = ix.plan_renames(out, idx)
    ix.apply_renames(out, idx, plans, copy_media=copies)
    dst = copies / "20990304_가상-REC00010.MP3"
    assert dst.read_bytes() == (media / "REC00010.MP3").read_bytes()
    assert (media / "REC00010.MP3").exists()


def test_outputs_of_prefers_longer_stem(tmp_path):
    for n in ("회의.raw.md", "회의.v1.raw.md", "회의.segments.json", "회의.v1.segments.json"):
        (tmp_path / n).write_text("")
    got = [p.name for p in ix.outputs_of(tmp_path, "회의", {"회의", "회의.v1"})]
    assert got == ["회의.raw.md", "회의.segments.json"]


def test_weekday_years():
    import datetime as dt
    # 2099-03-04는 수요일
    assert 2099 in ix.weekday_years(3, 4, "수", today=dt.date(2099, 12, 31))
    assert ix.weekday_years(2, 30, "월", today=dt.date(2099, 1, 1)) == []
    t = Transcript("x", 60, "local", "ko", [Segment(0, 5, "3월 4일 수요일에 봅시다")])
    c = [c for c in ix.find_clues(t) if c["kind"] == "date"][0]
    assert c["weekday"] == "수" and isinstance(c["years"], list)


def test_rename_never_touches_media_beside_outputs(tmp_path):
    # rec2md <파일>의 기본처럼 출력이 원본 옆에 있는 경우: 원본 미디어는 계획에서 빠진다
    d = tmp_path / "rec"
    d.mkdir()
    (d / "REC00010.MP3").write_bytes(b"audio")
    (d / "REC00010.wav").write_bytes(b"audio2")
    write_seg(d, "REC00010", "REC00010.MP3")
    ix.build(d)
    _approve(d, "REC00010", date="2099-03-04", date_precision="day", date_confidence="high", topic="가상")
    idx = ix.load_index(d)
    plans = ix.plan_renames(d, idx)
    assert not any(f.lower().endswith((".mp3", ".wav")) for f in plans[0]["files"])
    ix.apply_renames(d, idx, plans)
    assert (d / "REC00010.MP3").read_bytes() == b"audio" and (d / "REC00010.wav").exists()


def test_fingerprint_covers_tail_between_1_and_2_mib(tmp_path):
    p = tmp_path / "a.mp3"
    body = bytearray(b"\0" * (ix.FP_CHUNK + 1000))
    p.write_bytes(bytes(body))
    before = ix.fingerprint(p)
    body[-1] = 1  # 같은 크기, 꼬리만 바뀜
    p.write_bytes(bytes(body))
    assert ix.fingerprint(p) != before


def test_day_without_confidence_is_downgraded(rec):
    _, out = rec
    idx = ix.build(out)
    item = ix.find_item(idx, "REC00012")
    ix.set_agent(item, date="2099-01-06", date_precision="day", topic="가상")
    assert item["agent"]["date_precision"] == "month"


def test_revalidate_clears_stale_but_not_approval(rec):
    media, out = rec
    ix.build(out)
    _approve(out, "REC00012", date="2099", date_precision="year", date_confidence="low", topic="가상")
    (media / "REC00012.MP3").write_bytes(b"other recording")
    ix.build(out)
    assert ix.find_item(ix.load_index(out), "REC00012")["stale"]
    index_main(["set", str(out), "REC00012", "--topic", "가상 재검토", "--revalidate"])
    item = ix.find_item(ix.build(out), "REC00012")
    assert not item["stale"] and not item["approved"]


def test_batch_reserves_renamed_output_stem(rec):
    from rec2md import cmd_batch
    media, out = rec
    ix.build(out)
    _approve(out, "REC00010", date="2099-03-04", date_precision="day", date_confidence="high", topic="가상")
    idx = ix.load_index(out)
    ix.apply_renames(out, idx, ix.plan_renames(out, idx))
    state = json.loads((out / ".rec2md-state.json").read_text(encoding="utf-8"))
    newcomer = media / "20990304_가상-REC00010.MP3"  # 새 녹음이 우연히 바뀐 출력 이름과 같다
    newcomer.write_bytes(b"new")
    clash = cmd_batch._clashes([media / "REC00010.MP3", newcomer], state)
    assert newcomer in clash and media / "REC00010.MP3" not in clash


def test_undo_last_of_two_renames_keeps_reservation(rec):
    _, out = rec
    ix.build(out)
    _approve(out, "REC00010", date="2099-03-04", date_precision="day", date_confidence="high", topic="첫이름")
    idx = ix.load_index(out)
    ix.apply_renames(out, idx, ix.plan_renames(out, idx))
    _approve(out, "REC00010.MP3", date="2099-03-04", date_precision="day", date_confidence="high", topic="둘째이름")
    idx = ix.load_index(out)
    ix.apply_renames(out, idx, ix.plan_renames(out, idx))
    ix.undo_renames(out, ix.load_index(out), 1)
    state = json.loads((out / ".rec2md-state.json").read_text(encoding="utf-8"))
    assert state["REC00010.MP3"]["outputs"] == "20990304_첫이름-REC00010"
    ix.undo_renames(out, ix.load_index(out), 1)
    state = json.loads((out / ".rec2md-state.json").read_text(encoding="utf-8"))
    assert "outputs" not in state["REC00010.MP3"]


def test_duplicate_outputs_use_newest_and_block_rename(rec):
    import os
    _, out = rec
    ix.build(out)
    _approve(out, "REC00010", date="2099-03-04", date_precision="day", date_confidence="high", topic="가상")
    idx = ix.load_index(out)
    ix.apply_renames(out, idx, ix.plan_renames(out, idx))
    # 바뀐 원본을 다시 전사하면 원래 이름으로 새 출력이 생긴다
    write_seg(out, "REC00010", "REC00010.MP3", duration=999.0)
    old = out / "20990304_가상-REC00010.segments.json"
    os.utime(old, (1, 1))
    item = ix.find_item(ix.build(out), "REC00010.MP3")
    assert item["duration"] == 999.0 and item["stem"] == "REC00010"
    assert set(item["conflict"]) == {"REC00010", "20990304_가상-REC00010"}
    item["approved"] = True
    plans = ix.plan_renames(out, {"items": [item]})
    assert "여럿" in plans[0]["skip"]
