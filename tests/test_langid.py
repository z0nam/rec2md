"""`--language auto`: 창 고르기, 창·녹음 판정, run_one·batch 연결. 모델 없이 가짜 판별기로 돈다."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from rec2md import audio, cli, cmd_batch, langid
from rec2md.segments import Segment

SR = audio.SAMPLE_RATE


def test_windows_follow_speech_not_wall_clock():
    # 말소리 120초가 긴 무음 사이에 흩어져 있다 → 이어 붙인 축의 0·45·90초에서 30초씩
    regions = [(0.0, 10.0), (100.0, 150.0), (200.0, 260.0)]
    w = langid.window_spans(regions)
    assert w == [[(0.0, 10.0), (100.0, 120.0)], [(135.0, 150.0), (200.0, 215.0)], [(230.0, 260.0)]]
    assert all(abs(sum(e - s for s, e in p) - 30.0) < 1e-9 for p in w)


def test_short_recording_has_one_window():
    assert langid.window_spans([(5.0, 20.0)]) == [[(5.0, 20.0)]]
    assert langid.window_spans([]) == []


@pytest.mark.parametrize("probs,text,want", [
    ({"ko": 0.95, "en": 0.05}, "안녕하세요 오늘 회의를 시작하겠습니다", "ko"),
    ({"en": 0.7, "ko": 0.3}, "Thank you all for coming to this session today", "en"),
    # 확률은 en인데 받아 적은 글자가 한글이면 글자를 믿는다
    ({"en": 0.6, "ko": 0.4}, "네 그렇게 하시죠 다음 안건으로 넘어가겠습니다", "ko"),
    # 글자가 너무 적으면 확률이 높을 때만 믿는다
    ({"en": 0.95}, "Yes", "en"),
    ({"es": 0.15, "en": 0.1}, "음", "?"),
    # 한글·라틴이 섞이면 판단을 미룬다
    ({"ko": 0.9}, "이번 발표는 AI platform 로드맵입니다", "?"),
])
def test_window_lang(probs, text, want):
    assert langid.window_lang(probs, text)[0] == want


def W(lang, prob):
    return {"lang": lang, "prob": prob}


def test_decide():
    assert langid.decide([W("en", 0.99), W("en", 0.99), W("?", 0.7)]) == ("en", "en")
    # 끝 창의 낮은 확률 en은 확실한 ko 창이 있으면 잡음으로 보고 뺀다
    assert langid.decide([W("ko", 0.99), W("ko", 1.0), W("en", 0.4)]) == ("ko", "ko")
    # 확실한 창끼리 다르면 섞인 것 → ko로 전사
    assert langid.decide([W("ko", 0.94), W("en", 1.0), W("ko", 0.93)]) == ("mixed", "ko")
    assert langid.decide([W("silent", 0.0), W("?", 0.3)]) == ("silent", "ko")
    # 확실한 창이 하나도 없으면 약한 창끼리 판단한다
    assert langid.decide([W("en", 0.5), W("en", 0.55)]) == ("en", "en")


def tone(sec, amp=0.1):
    t = np.arange(int(sec * SR)) / SR
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def test_detect_skips_silent_windows_and_records_them():
    seen = []

    def detector(clip):
        seen.append(len(clip) / SR)
        return {"en": 0.9, "ko": 0.1}, "Good morning everyone and welcome"

    a = np.concatenate([tone(40), np.zeros(40 * SR, np.float32), tone(40)])
    det = langid.detect(a, None, detector)   # 구간을 모르면 파일 전체의 처음·중간·끝
    assert [w["lang"] for w in det["windows"]] == ["en", "silent", "en"]
    assert (det["verdict"], det["language"]) == ("en", "en")
    assert seen == [30.0, 30.0]
    assert langid.summary(det) == "en (en 0.90, 무음, en 0.90)"


@pytest.fixture
def fake_run(tmp_path, monkeypatch):
    """ffmpeg·모델 없이 run_one을 돌린다. 판별기는 창마다 LANGS를 차례로 돌려준다."""
    src = tmp_path / "talk.wav"
    audio.write_wav(src, tone(100))
    monkeypatch.setattr(audio, "normalize", lambda s, d: Path(shutil.copy(s, d)))
    monkeypatch.setattr(audio, "probe_duration", lambda p: 100.0)
    calls = {"langs": [], "detect": 0}

    class Engine:
        name, model = "fake", "m"

        def detect_language(self, clip):
            lang = calls["seq"][calls["detect"] % len(calls["seq"])]
            calls["detect"] += 1
            return {lang: 0.95}, {"ko": "오늘 회의는 여기까지 하겠습니다", "en": "That is all for today thank you"}[lang]

        def transcribe(self, wav, language, regions=None):
            calls["langs"].append(language)
            return [Segment(0.0, 2.0, "hello")]

    monkeypatch.setattr(cli, "get_engine", lambda *a, **k: Engine())
    return src, calls


def run(src, *opts):
    args = cli.build_parser().parse_args([str(src), "--no-diarize", "--no-trim", *opts])
    cli.run_one(src, args)
    return json.loads(src.with_name("talk.segments.json").read_text(encoding="utf-8"))


def test_auto_transcribes_in_detected_language(fake_run):
    src, calls = fake_run
    calls["seq"] = ["en"]
    out = run(src, "--language", "auto")
    assert calls["langs"] == ["en"] and out["language"] == "en"
    det = out["meta"]["language_detect"]
    assert (det["verdict"], det["language"], len(det["windows"])) == ("en", "en", 3)


def test_mixed_falls_back_to_korean_and_says_so(fake_run):
    src, calls = fake_run
    calls["seq"] = ["ko", "en", "ko"]
    out = run(src, "--language", "auto")
    assert calls["langs"] == ["ko"] and out["language"] == "ko"
    assert out["meta"]["language_detect"]["verdict"] == "mixed"


def test_fixed_language_does_not_detect(fake_run):
    src, calls = fake_run
    calls["seq"] = ["en"]
    out = run(src)
    assert calls == {"langs": ["ko"], "detect": 0, "seq": ["en"]}
    assert "language_detect" not in out["meta"]


def test_dry_run_shows_language_column(tmp_path, monkeypatch, capsys):
    d = tmp_path / "rec"
    d.mkdir()
    for name in ("a.mp3", "b.mp3"):
        (d / name).write_bytes(b"")
    monkeypatch.setattr(cmd_batch.audio, "probe_duration", lambda p: 600.0)
    monkeypatch.setattr(cmd_batch, "speech_sec", lambda p, min_gap=0: 600.0)
    monkeypatch.setattr(cmd_batch.trim, "loudness_file", lambda p: np.zeros(1))
    monkeypatch.setattr(cmd_batch.trim, "plan", lambda db, gap: type("P", (), {"keep": [(0.0, 600.0)]}))
    monkeypatch.setattr(cmd_batch.speechcheck, "_clip", lambda f, a, n: tone(n))
    seq = {"a.mp3": ["en"], "b.mp3": ["ko", "en", "ko"]}
    state = {"i": 0, "f": None}

    def detector(clip):
        langs = seq[state["f"]]
        lang = langs[state["i"] % len(langs)]
        state["i"] += 1
        return {lang: 0.95}, {"ko": "오늘 회의는 여기까지 하겠습니다", "en": "That is all for today thank you"}[lang]

    items = cmd_batch.plan(cmd_batch.media_files(d), {}, 30.0)
    for it in items:
        state["f"], state["i"] = it.path.name, 0
        cmd_batch.check_language([it], 300.0, detector=detector)
    rep = cmd_batch.report(items)
    assert "언어" in rep.splitlines()[0]
    assert "  en  " in rep.splitlines()[2] and "mixed→ko" in rep.splitlines()[3]
    assert "mixed·silent 1개는 ko로 전사" in rep


def test_dry_run_without_auto_has_no_language_column(tmp_path, monkeypatch):
    d = tmp_path / "rec"
    d.mkdir()
    (d / "a.mp3").write_bytes(b"")
    monkeypatch.setattr(cmd_batch.audio, "probe_duration", lambda p: 600.0)
    monkeypatch.setattr(cmd_batch, "speech_sec", lambda p, min_gap=0: 600.0)
    monkeypatch.setattr(cmd_batch, "check_language", lambda *a, **k: pytest.fail("auto가 아니면 판별하지 않음"))
    monkeypatch.setattr("sys.argv", ["rec2md"])
    cmd_batch.main([str(d), "--dry-run"])
