"""speechcheck.windows: 남긴 구간 위에 표본 창을 고르게 놓는다(모델 없이)."""

from rec2md import speechcheck


def test_windows_spread_over_kept_regions_and_stay_inside():
    keep = [(0.0, 600.0), (3600.0, 4200.0)]   # 10분 + 10분, 사이 50분은 잘라냄
    ws = speechcheck.windows(keep, n=4, win=30.0)
    assert len(ws) == 4
    assert sum(1 for s, _ in ws if s < 600) == 2 and sum(1 for s, _ in ws if s >= 3600) == 2
    for s, d in ws:
        assert d == 30.0
        assert any(a <= s and s + d <= b for a, b in keep)


def test_windows_clamp_to_short_region_and_edges():
    ws = speechcheck.windows([(100.0, 110.0)], n=3, win=30.0)
    assert ws == [(100.0, 10.0)] * 3
    ws = speechcheck.windows([(0.0, 3600.0)], n=1, win=30.0)
    assert ws == [(1785.0, 30.0)]
    assert speechcheck.windows([], n=5) == []


class _Ann:
    def get_timeline(self):
        return self

    def support(self):
        return self

    def duration(self):
        return 3.0


def test_speech_ratio_fails_instead_of_zero_when_nothing_decodes(monkeypatch, tmp_path):
    """--no-trim에서 길이는 읽히는데 소리는 못 읽는 파일: 0% 말소리가 아니라 실패로 알린다(Codex #43)."""
    import numpy as np
    import pytest

    pytest.importorskip("torch")
    monkeypatch.setattr(speechcheck, "_load_vad", lambda: (lambda audio: _Ann()))
    monkeypatch.setattr(speechcheck, "probe_duration", lambda p: 600.0)
    monkeypatch.setattr(speechcheck, "_clip", lambda p, s, d: np.zeros(0, dtype=np.float32))
    with pytest.raises(RuntimeError):
        speechcheck.speech_ratio(tmp_path / "x.mp3", trimmed=False)
    # 일부라도 읽히면 읽힌 것만으로 비율을 낸다
    monkeypatch.setattr(speechcheck, "_clip", lambda p, s, d: np.zeros(int(d * speechcheck.SAMPLE_RATE), np.float32))
    kept, ratio = speechcheck.speech_ratio(tmp_path / "x.mp3", trimmed=False, n=2)
    assert kept == 600.0 and abs(ratio - 0.1) < 1e-9


def test_clip_raises_on_decoder_failure(monkeypatch, tmp_path):
    import subprocess

    import pytest

    monkeypatch.setattr(speechcheck, "_require", lambda t: t)
    monkeypatch.setattr(speechcheck.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout=b"", stderr=b"Invalid data"))
    with pytest.raises(RuntimeError, match="Invalid data"):
        speechcheck._clip(tmp_path / "x.mp3", 0.0, 30.0)
