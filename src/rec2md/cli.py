"""rec2md <audio|video> → <이름>.segments.json + <이름>.raw.md"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv

from . import audio
from .clean import collapse_repeats
from .engines import get_engine
from .render import fmt_ts, render_raw
from .segments import Transcript

ENV_FILE = Path.home() / ".config" / "rec2md" / ".env"


def log(msg: str) -> None:
    print(f"[rec2md] {msg}", file=sys.stderr, flush=True)


def run_one(src: Path, args) -> Path:
    out_dir = Path(args.out) if args.out else src.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    # with_suffix는 "meeting.v1"의 ".v1"을 확장자로 보고 갈아끼우므로 이름 뒤에 덧붙인다
    seg_path, raw_path = out_dir / f"{src.stem}.segments.json", out_dir / f"{src.stem}.raw.md"

    duration = audio.probe_duration(src)
    log(f"{src.name} ({fmt_ts(duration)})")
    meta: dict = {}
    with tempfile.TemporaryDirectory(prefix="rec2md-") as td:
        wav = audio.normalize(src, Path(td) / "audio.wav")

        t0 = time.monotonic()
        engine = get_engine(args.engine, model=args.model)
        segs = collapse_repeats(engine.transcribe(wav, args.language))
        meta["asr_sec"] = round(time.monotonic() - t0, 1)
        log(f"전사 {meta['asr_sec']}s (실시간 대비 {duration / max(meta['asr_sec'], 0.1):.1f}배)")

        if not args.no_diarize:
            from .diarize import PIPELINE, assign_speakers, diarize
            t0 = time.monotonic()
            turns = diarize(wav, args.speakers)
            segs = assign_speakers(segs, turns)
            meta["diarizer"] = PIPELINE.split("/")[-1]
            meta["diarize_sec"] = round(time.monotonic() - t0, 1)
            log(f"화자분리 {meta['diarize_sec']}s")

    t = Transcript(source=src.name, duration=duration, engine=f"{engine.name}:{getattr(engine, 'model', '')}",
                   language=args.language, segments=segs, meta=meta)
    t.to_json(seg_path)
    raw_path.write_text(render_raw(t, src.stem), encoding="utf-8")
    log(f"→ {raw_path}")
    return raw_path


def main(argv: list[str] | None = None) -> None:
    load_dotenv(ENV_FILE)
    p = argparse.ArgumentParser(prog="rec2md", description="녹음·영상 → 화자 구분 녹취록 Markdown")
    p.add_argument("inputs", nargs="+", type=Path, help="오디오/영상 파일 (mp3, m4a, wav, mp4, mov …)")
    p.add_argument("--engine", default="local", choices=["local"])
    p.add_argument("--model", help="엔진별 모델 (local 기본: whisper-large-v3-turbo)")
    p.add_argument("--language", default="ko")
    p.add_argument("--speakers", type=int, help="화자 수를 알면 지정 (정확도 향상)")
    p.add_argument("--no-diarize", action="store_true", help="화자분리 생략")
    p.add_argument("--out", help="출력 폴더 (기본: 원본 옆)")
    args = p.parse_args(argv)

    for src in args.inputs:
        if not src.is_file():
            raise SystemExit(f"파일 없음: {src}")
        run_one(src, args)


if __name__ == "__main__":
    main()
