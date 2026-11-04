"""local 엔진의 mlx 캐시 상한. mlx·mlx_whisper를 가짜 모듈로 바꿔 끼워 모델 없이 돈다."""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from rec2md import audio
from rec2md.engines import local_mlx


@pytest.fixture
def fake_mlx(monkeypatch):
    calls: list = []
    state = {"limit": 99}

    def set_cache_limit(n):
        calls.append(("limit", n))
        prev, state["limit"] = state["limit"], n
        return prev

    core = types.SimpleNamespace(set_cache_limit=set_cache_limit, clear_cache=lambda: calls.append(("clear",)))
    mlx = types.ModuleType("mlx")
    mlx.core = core
    whisper = types.ModuleType("mlx_whisper")

    def transcribe(audio, **kw):
        calls.append(("decode", state["limit"]))
        if state.get("fail"):
            raise RuntimeError("boom")
        return {"segments": [{"start": 0.0, "end": 1.0, "text": " 안녕", "words": []}]}

    whisper.transcribe = transcribe
    monkeypatch.setitem(sys.modules, "mlx", mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", core)
    monkeypatch.setitem(sys.modules, "mlx_whisper", whisper)
    return calls, state


@pytest.fixture
def wav(tmp_path):
    return audio.write_wav(tmp_path / "a.wav", np.zeros(audio.SAMPLE_RATE * 3, dtype=np.float32))


def test_decodes_under_cache_limit_then_restores(fake_mlx, wav):
    calls, state = fake_mlx
    segs = local_mlx.LocalMLX().transcribe(wav, "ko", [(0.0, 1.0), (1.5, 2.5)])
    assert [s.text for s in segs] == ["안녕", "안녕"]
    assert calls[0] == ("limit", local_mlx.CACHE_LIMIT)
    assert [c for c in calls if c[0] == "decode"] == [("decode", local_mlx.CACHE_LIMIT)] * 2
    assert calls[-2:] == [("limit", 99), ("clear",)] and state["limit"] == 99


def test_restores_limit_when_decoding_fails(fake_mlx, wav):
    calls, state = fake_mlx
    state["fail"] = True
    with pytest.raises(RuntimeError):
        local_mlx.LocalMLX().transcribe(wav, "ko", None)
    assert calls[-2:] == [("limit", 99), ("clear",)] and state["limit"] == 99
