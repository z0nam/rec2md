"""segments → raw transcript Markdown (타임스탬프 + 화자 라벨)."""

from __future__ import annotations

from .segments import Transcript


def fmt_ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def render_raw(t: Transcript, title: str) -> str:
    lines = [
        f"# {title}",
        "",
        f"- 원본: `{t.source}`",
        f"- 길이: {fmt_ts(t.duration)}",
        f"- 엔진: {t.engine}" + (f" / 화자분리: {t.meta['diarizer']}" if t.meta.get("diarizer") else ""),
    ]
    speakers = sorted({s.speaker for s in t.segments if s.speaker})
    if speakers:
        lines.append(f"- 화자: {len(speakers)}명 ({', '.join(speakers)}) — 라벨은 자동 부여, 실명 아님")
    lines += ["", "---", ""]

    # 같은 화자가 이어 말하면 한 문단으로 합친다
    cur_spk, cur_start, buf = object(), 0.0, []

    def flush():
        if not buf:
            return
        label = f" {cur_spk}" if cur_spk else ""
        lines.append(f"**[{fmt_ts(cur_start)}]{label}** {' '.join(buf)}")
        lines.append("")

    for s in t.segments:
        text = s.text.strip()
        if not text:
            continue
        # 화자 정보가 없으면(--no-diarize) 세그먼트마다 끊어 타임스탬프를 살린다
        if s.speaker != cur_spk or s.speaker is None:
            flush()
            cur_spk, cur_start, buf = s.speaker, s.start, []
        buf.append(text)
    flush()
    return "\n".join(lines)
