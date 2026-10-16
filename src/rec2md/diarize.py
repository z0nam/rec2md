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

    import soundfile as sf  # pyannote 의존성으로 같이 설치됨

    # 파일 경로 대신 파형을 넘기면 torchcodec 디코딩 문제를 피한다
    data, sr = sf.read(str(wav), dtype="float32")
    audio = {"waveform": torch.from_numpy(data).unsqueeze(0), "sample_rate": sr}
    kw = {"num_speakers": num_speakers} if num_speakers else {}
    out = pipe(audio, **kw)
    # community-1은 겹침 발화를 빼고 한 시점에 한 화자만 남긴 결과를 따로 준다(전사 정렬용)
    ann = getattr(out, "exclusive_speaker_diarization", None) or getattr(out, "speaker_diarization", out)
    return [(seg.start, seg.end, spk) for seg, _, spk in ann.itertracks(yield_label=True)]


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
    out: list[Segment] = []
    for seg in segs:
        words = seg.words or [Word(seg.start, seg.end, seg.text)]
        for w in words:
            raw = _speaker_at(w, turns)
            if raw is not None and raw not in rename:
                rename[raw] = f"화자{len(rename) + 1}"
            w.speaker = rename.get(raw)
        cur: list[Word] = []
        for w in words:
            if cur and w.speaker != cur[-1].speaker:
                out.append(_join(cur))
                cur = []
            cur.append(w)
        if cur:
            out.append(_join(cur))
    return out


def _join(words: list[Word]) -> Segment:
    return Segment(
        start=words[0].start, end=words[-1].end,
        text="".join(w.text for w in words).strip(),
        speaker=words[0].speaker, words=words,
    )
