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


def test_dry_run_detects_with_selected_model(tmp_path, monkeypatch):
    d = tmp_path / "rec"
    d.mkdir()
    (d / "a.mp3").write_bytes(b"")
    monkeypatch.setattr(cmd_batch.audio, "probe_duration", lambda p: 600.0)
    monkeypatch.setattr(cmd_batch, "speech_sec", lambda p, min_gap=0: 600.0)
    seen = {}
    monkeypatch.setattr(cmd_batch, "check_language", lambda items, gap, trimmed=True, model=None: seen.update(model=model))
    cmd_batch.main([str(d), "--dry-run", "--language", "auto", "--model", "my/whisper"])
    assert seen == {"model": "my/whisper"}
# ---- 구간별 언어 (--mixed-language per-region) ----

def test_region_picker_switches_only_when_sure_and_among_candidates():
    probs = iter([{"ko": 0.9, "en": 0.1}, {"en": 0.95, "ko": 0.05}, {"en": 0.7, "ko": 0.3},
                  {"ja": 0.9, "en": 0.05, "ko": 0.05}])
    p = langid.RegionPicker(lambda clip: next(probs), ["ko", "en"])
    clip = np.zeros(SR, np.float32)
    got = [p(clip, 0, 10), p(clip, 10, 20), p(clip, 20, 30), p(clip, 30, 40)]
    # 확신이 낮은 en(0.7)과 후보 밖 언어(ja)는 ko로 둔다
    assert got == ["ko", "en", "ko", "ko"]
    assert p.runs == [[0, 10, "ko"], [10, 20, "en"], [20, 40, "ko"]]
    assert p.meta()["sec"] == {"ko": 30, "en": 10}


def test_region_picker_short_chunk_follows_previous():
    probs = iter([{"en": 0.99}])
    p = langid.RegionPicker(lambda clip: next(probs), ["ko", "en"])
    clip = np.zeros(SR, np.float32)
    assert [p(clip, 0, 10), p(clip, 10, 11.5)] == ["en", "en"]   # 1.5초 "Thank you"는 앞 청크를 따른다
    assert p.runs == [[0, 11.5, "en"]]


def test_candidates_come_from_sure_windows():
    det = {"windows": [W("ko", 0.94), W("en", 1.0), W("ja", 0.3), W("silent", 0.0)]}
    assert langid.candidates_of(det) == ["ko", "en"]


@pytest.fixture
def mixed_run(fake_run, monkeypatch):
    """화자분리를 가짜로 바꿔 청크 3개(0-20, 30-55, 70-95초)를 만든다. 창 판정은 ko, en, ko(섞임)."""
    from rec2md import diarize
    turns = [(0.0, 20.0, "A"), (30.0, 55.0, "B"), (70.0, 95.0, "A")]
    monkeypatch.setattr(diarize, "diarize_with_embeddings", lambda wav, n=None: (turns, {}, []))
    monkeypatch.setattr(diarize, "speech_regions", lambda t: [(s, e) for s, e, _ in t])
    src, calls = fake_run
    calls["seq"] = ["ko", "en", "ko"]
    chunk_probs = iter([{"ko": 0.9}, {"en": 0.97, "ko": 0.03}, {"ko": 0.8}])

    class Engine:
        name, model = "fake", "m"

        def detect_language(self, clip):
            lang = calls["seq"][calls["detect"] % 3]
            calls["detect"] += 1
            return {lang: 0.95}, {"ko": "오늘 회의는 여기까지 하겠습니다", "en": "That is all for today thank you"}[lang]

        def language_probs(self, clip):
            return next(chunk_probs)

        def transcribe(self, wav, language, regions=None):
            for s, e in regions:
                calls["langs"].append(language(np.zeros(int((e - s) * SR), np.float32), s, e)
                                      if callable(language) else language)
            return [Segment(0.0, 2.0, "hello")]

    monkeypatch.setattr(cli, "get_engine", lambda *a, **k: Engine())
    return src, calls


def test_per_region_transcribes_each_chunk_in_its_language(mixed_run):
    src, calls = mixed_run
    args = cli.build_parser().parse_args([str(src), "--no-trim", "--no-voices", "--language", "auto",
                                          "--mixed-language", "per-region"])
    cli.run_one(src, args)
    out = json.loads(src.with_name("talk.segments.json").read_text(encoding="utf-8"))
    assert calls["langs"] == ["ko", "en", "ko"]
    reg = out["meta"]["language_detect"]["regions"]
    assert reg["runs"] == [[0.0, 20.0, "ko"], [30.0, 55.0, "en"], [70.0, 95.0, "ko"]]
    assert reg["sec"] == {"ko": 45.0, "en": 25.0}
    assert out["language"] == "ko"


def test_mixed_without_per_region_stays_korean(mixed_run):
    src, calls = mixed_run
    args = cli.build_parser().parse_args([str(src), "--no-trim", "--no-voices", "--language", "auto"])
    cli.run_one(src, args)
    out = json.loads(src.with_name("talk.segments.json").read_text(encoding="utf-8"))
    assert calls["langs"] == ["ko", "ko", "ko"]
    assert "regions" not in out["meta"]["language_detect"]


def test_split_long_cuts_at_quiet_point():
    a = tone(70)
    a[int(22.0 * SR):int(22.3 * SR)] = 0.0   # 22초 근처의 쉼
    a[int(41.0 * SR):int(41.3 * SR)] = 0.0
    out = langid.split_long([(0.0, 70.0), (80.0, 90.0)], np.concatenate([a, tone(20)]))
    assert [round(s, 1) for s, _ in out[:3]] == [0.0, 22.1, 41.1]
    assert all(e - s <= 25.0 for s, e in out)
    assert out[-1] == (80.0, 90.0) and out[-2][1] == 70.0
