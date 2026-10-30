"""전사 전 긴 무음 자르기(trim): 판정, 오프셋 매핑, run_one 연결. 합성 데이터만 쓴다."""

from __future__ import annotations

import numpy as np
import pytest

from rec2md import audio, cli, trim
from rec2md.segments import Segment, Word

SPEECH, NOISE = -18.0, -52.0  # 360분 실측: 말소리 -10〜-25dB, 녹음기 "무음" -50〜-57dB


def profile(*parts: tuple[str, int]) -> np.ndarray:
    """("s"|"n", 초) 조각으로 1초 단위 dB 배열을 만든다. 잡음에는 가끔 튀는 소리를 섞는다."""
    out = []
    rng = np.random.default_rng(0)
    for kind, sec in parts:
        if kind == "s":
            out.append(SPEECH + rng.normal(0, 4, sec))
        else:
            x = NOISE + rng.normal(0, 2, sec)
            x[::97] = -38.0  # 문 닫는 소리 같은 드문 튐 — 말소리로 치면 안 된다
            out.append(x)
    return np.concatenate(out).astype(np.float32)


def test_cuts_only_long_silence():
    db = profile(("s", 1800), ("n", 240), ("s", 600), ("n", 3 * 3600), ("s", 300), ("n", 1200))
    p = trim.plan(db)
    assert T_MIN_OK(p.threshold)
    # 4분 무음은 남기고(5분 미만), 3시간·20분 무음은 자른다
    assert len(p.cut) == 2
    (a0, a1), (b0, b1) = p.cut
    assert 2640 < a0 < 2700 + 60 and abs(a1 - (2640 + 3 * 3600)) <= 60 + trim.PAD
    assert b1 == len(db)
    kept = p.kept_sec
    assert 2640 + 300 <= kept <= 2640 + 300 + 4 * (trim.WINDOW + trim.PAD)


def T_MIN_OK(t: float) -> bool:
    return trim.T_MIN <= t <= trim.T_MAX


def test_nothing_to_cut():
    assert not trim.plan(profile(("s", 3600))).trims()
    assert not trim.plan(profile(("s", 600), ("n", 200), ("s", 600))).trims()  # 5분 미만 무음
    assert not trim.plan(profile(("n", 3600))).trims()  # 말소리를 못 찾으면 자르지 않는다


def test_leading_silence_is_cut_when_long():
    p = trim.plan(profile(("n", 900), ("s", 600)))
    assert p.cut and p.cut[0][0] == 0.0 and p.keep[-1][1] == 1500


def test_offset_map_round_trip():
    m = trim.OffsetMap([(0.0, 100.0), (400.0, 450.0), (1000.0, 1100.0)])
    assert m.length == 250
    assert m.to_original(0) == 0 and m.to_original(99.5) == 99.5
    assert m.to_original(100) == 400 and m.to_original(120) == 420
    assert m.to_original(150) == 1000 and m.to_original(250) == 1100
    assert m.table() == [[0, 0, 100], [100, 400, 450], [150, 1000, 1100]]


def test_remap_segments_and_spans():
    m = trim.OffsetMap([(0.0, 10.0), (500.0, 520.0)])
    seg = Segment(12.0, 15.0, "안녕", "화자1", words=[Word(12.0, 13.0, "안", 0.3)], low_conf=[[12.0, 13.0, 0.3, "안"]])
    trim.remap_segments([seg], m)
    assert (seg.start, seg.end) == (502.0, 505.0)
    assert (seg.words[0].start, seg.words[0].end) == (502.0, 503.0)
    assert seg.low_conf[0][:2] == [502.0, 503.0]
    assert trim.remap_spans([(11.0, 14.0, "SPEAKER_00")], m) == [(501.0, 504.0, "SPEAKER_00")]


def _tone(sec: float, amp: float) -> np.ndarray:
    t = np.arange(int(sec * audio.SAMPLE_RATE)) / audio.SAMPLE_RATE
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def test_trim_silence_writes_shorter_wav(tmp_path):
    """소리 3분 + 잡음 10분 + 소리 2분 → 10분 무음을 빼고 이어 붙이고, meta에 잘라낸 구간과 오프셋을 남긴다."""
    rng = np.random.default_rng(1)
    noise = (rng.normal(0, 10 ** (NOISE / 20), 600 * audio.SAMPLE_RATE)).astype(np.float32)
    data = np.concatenate([_tone(180, 0.2), noise, _tone(120, 0.2)])
    src = audio.write_wav(tmp_path / "in.wav", data)
    meta: dict = {}
    out, m = cli._trim_silence(src, tmp_path / "trimmed.wav", meta)
    assert out == tmp_path / "trimmed.wav" and m is not None
    kept = len(audio.load_wav(out)) / audio.SAMPLE_RATE
    assert 300 <= kept < 300 + 4 * trim.PAD + 1
    (c0, c1), = meta["trim"]["cut"]
    assert 180 <= c0 <= 180 + trim.PAD + trim.WINDOW and 780 - trim.PAD - trim.WINDOW <= c1 <= 780
    assert m.to_original(kept - 1) == pytest.approx(899, abs=1)  # 끝은 원본 끝으로


def test_trim_silence_noop_returns_original(tmp_path):
    src = audio.write_wav(tmp_path / "in.wav", _tone(120, 0.2))
    meta: dict = {}
    assert cli._trim_silence(src, tmp_path / "t.wav", meta) == (src, None)
    assert "trim" not in meta and not (tmp_path / "t.wav").exists()


def test_cli_has_no_trim_option():
    args = cli.build_parser().parse_args(["x.mp3", "--no-trim"])
    assert args.no_trim is True


def test_review_marks_cut_windows_with_faint_sound():
    """잘라낸 구간 안에 약한 소리가 몰린 1분 창은 meta.trim.review로 남긴다(먼 말소리일 수 있음)."""
    db = profile(("s", 600), ("n", 1800), ("s", 600))
    faint = 600 + 900                     # 잘리는 구간 한가운데 1분
    db[faint:faint + 60:12] = -20.0       # 1분에 5번(약 8%): 말소리 기준(10%) 아래, 다시 들어 볼 기준(5%) 위
    p = trim.plan(db)
    assert p.cut and p.review
    assert any(s < faint + 60 and e > faint for s, e in p.review)  # 그 1분과 겹치는 창
    m = trim.OffsetMap(p.keep)
    assert trim.meta(p, m)["review"]
    assert not trim.plan(profile(("s", 600), ("n", 1800), ("s", 600))).review  # 드문 튐만 있으면 없음
