"""화자분리(pyannote)와 전사 결과 병합."""

from __future__ import annotations

import os
from pathlib import Path

from .segments import Segment, Word

PIPELINE = "pyannote/speaker-diarization-community-1"

Turn = tuple[float, float, str]  # (start, end, speaker)


def diarize(wav: Path, num_speakers: int | None = None) -> list[Turn]:
    try:
        import torch
        from pyannote.audio import Pipeline
    except ImportError as e:
        raise SystemExit("화자분리에는 pyannote가 필요함: uv tool install --editable '.[diarize]'") from e

    token = os.environ.get("HF_TOKEN")
    if not token:
        raise SystemExit(
            "HF_TOKEN이 없음. ~/.config/rec2md/.env에 HF_TOKEN=... 추가하고 "
            f"https://hf.co/{PIPELINE} 에서 약관 동의 필요. (화자분리 없이 쓰려면 --no-diarize)"
        )
    pipe = Pipeline.from_pretrained(PIPELINE, token=token)
    if torch.backends.mps.is_available():
        pipe.to(torch.device("mps"))

    from .audio import SAMPLE_RATE, load_wav

    # 파일 경로 대신 파형을 넘기면 torchcodec 디코딩 문제를 피한다
    audio = {"waveform": torch.from_numpy(load_wav(wav)).unsqueeze(0), "sample_rate": SAMPLE_RATE}
    kw = {"num_speakers": num_speakers} if num_speakers else {}
    out = pipe(audio, **kw)
    # community-1은 겹침 발화를 빼고 한 시점에 한 화자만 남긴 결과를 따로 준다(전사 정렬용)
    ann = getattr(out, "exclusive_speaker_diarization", None) or getattr(out, "speaker_diarization", out)
    return [(seg.start, seg.end, spk) for seg, _, spk in ann.itertracks(yield_label=True)]


LOW_PROB = 0.3


def _overlap(w: Word, turns: list[Turn]) -> float:
    return max((min(e, w.end) - max(s, w.start) for s, e, _ in turns), default=0.0)


def speech_regions(turns: list[Turn], max_gap: float = 2.0, max_len: float = 25.0) -> list[tuple[float, float]]:
    """화자 구간을 화자와 무관하게 이어 붙여 전사용 청크로 만든다.

    2초 미만으로 끊긴 구간은 합치되(짧은 청크도 Whisper는 30초 창 하나 비용이 든다) 한 청크가 25초를 넘지 않게 한다(Whisper 창 30초 이내).
    원래 25초보다 긴 단일 구간은 그대로 두고 Whisper가 알아서 나눠 처리한다.
    """
    out: list[list[float]] = []
    for s, e, _ in sorted(turns):
        if out and s - out[-1][1] < max_gap and max(e, out[-1][1]) - out[-1][0] <= max_len:
            out[-1][1] = max(out[-1][1], e)
        elif out and s < out[-1][1]:  # 겹침 발화인데 길이 한도를 넘으면 앞 청크 끝부터 이어서
            if e > out[-1][1]:
                out.append([out[-1][1], e])
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def _speaker_at(w: Word, turns: list[Turn]) -> str | None:
    best, best_ov = None, 0.0
    for s, e, spk in turns:
        if s > w.end:
            break
        ov = min(e, w.end) - max(s, w.start)
        if ov > best_ov:
            best, best_ov = spk, ov
    if best is None:  # 겹침이 없으면 가장 가까운 화자 구간
        mid = (w.start + w.end) / 2
        best = min(turns, key=lambda t: min(abs(t[0] - mid), abs(t[1] - mid)))[2] if turns else None
    return best


def assign_speakers(segs: list[Segment], turns: list[Turn]) -> list[Segment]:
    """단어마다 화자를 붙인 뒤 화자가 바뀌는 지점에서 세그먼트를 다시 자른다."""
    turns = sorted(turns)
    # 첫 등장 순서대로 화자1, 화자2 … 로 이름을 바꾼다
    rename: dict[str, str] = {}
    words = [w for seg in segs for w in (seg.words or [Word(seg.start, seg.end, seg.text)])]
    # 말소리 구간과 전혀 안 겹치고 확신도 낮은 단어는 무음·잡음에서 지어낸 것으로 보고 버린다
    words = [w for w in words if w.prob >= LOW_PROB or _overlap(w, turns) > 0]
    for w in words:
        raw = _speaker_at(w, turns)
        if raw is not None and raw not in rename:
            rename[raw] = f"화자{len(rename) + 1}"
        w.speaker = rename.get(raw)
    _smooth(words)

    # 화자가 바뀌거나, 원래 세그먼트 경계에서 말이 2초 넘게 끊기면 자른다
    out: list[Segment] = []
    cur: list[Word] = []
    for w in words:
        if cur and (w.speaker != cur[-1].speaker or w.start - cur[-1].end > 2.0):
            out.append(_join(cur))
            cur = []
        cur.append(w)
    if cur:
        out.append(_join(cur))
    return out


def _smooth(words: list[Word], max_words: int = 2, max_sec: float = 1.0) -> None:
    """앞뒤와 다른 화자로 잠깐 튄 짧은 구간(맞장구 제외 대부분 정렬 오차)을 앞 화자에 붙인다."""
    i = 0
    while i < len(words):
        j = i
        while j < len(words) and words[j].speaker == words[i].speaker:
            j += 1
        run = words[i:j]
        short = len(run) <= max_words and run[-1].end - run[0].start <= max_sec
        # 앞뒤가 같은 화자이고(A-B-C에서 짧은 B는 진짜 발화일 수 있음) 양쪽 모두와 붙어 있을 때만
        sandwiched = 0 < i and j < len(words) and words[j].speaker == words[i - 1].speaker
        glued = sandwiched and run[0].start - words[i - 1].end < 0.3 and words[j].start - run[-1].end < 0.3
        if short and glued:
            for w in run:
                w.speaker = words[i - 1].speaker
        i = j


def _join(words: list[Word]) -> Segment:
    return Segment(
        start=words[0].start, end=words[-1].end,
        text="".join(w.text for w in words).strip(),
        speaker=words[0].speaker, words=words,
    )
