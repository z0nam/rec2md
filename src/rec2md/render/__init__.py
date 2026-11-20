"""segments → raw transcript Markdown (타임스탬프 + 화자 라벨)."""

from __future__ import annotations

from .. import gaps
from ..segments import Transcript


def fmt_ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def speaker_label(spk: str, names: dict) -> str:
    """meta.speaker_names가 있으면 "홍길동(화자3)", 불확실하면 "홍길동?(화자3)"."""
    d = names.get(spk)
    if not d:
        return spk
    return f"{d['name']}{'?' if d.get('tentative') else ''}({spk})"


def render_raw(t: Transcript, title: str) -> str:
    lines = [
        f"# {title}",
        "",
        f"- 원본: `{t.source}`",
        f"- 길이: {fmt_ts(t.duration)}",
        f"- 엔진: {t.engine}" + (f" / 화자분리: {t.meta['diarizer']}" if t.meta.get("diarizer") else ""),
    ]
    speakers = sorted({s.speaker for s in t.segments if s.speaker})
    names = t.meta.get("speaker_names") or {}
    if speakers:
        note = "이름은 목소리 DB 매칭·사용자 지정(?는 불확실)" if names else "라벨은 자동 부여, 실명 아님"
        lines.append(f"- 화자: {len(speakers)}명 ({', '.join(speaker_label(x, names) for x in speakers)}) — {note}")
    sus = gaps.suspects(t)
    if sus:
        lines.append(f"- ⚠ 전사 누락 의심 {len(sus)}곳 — 말소리 구간인데 글이 거의 없음, 다시 들어 볼 것: "
                     + ", ".join(f"{fmt_ts(g['start'])}-{fmt_ts(g['end'])}" for g in sus))
    lines += ["", "---", ""]

    # 같은 화자가 이어 말하면 한 문단으로 합친다
    cur_spk, cur_start, buf = object(), 0.0, []

    def flush():
        if not buf:
            return
        label = f" {speaker_label(cur_spk, names)}" if cur_spk else ""
        lines.append(f"**[{fmt_ts(cur_start)}]{label}** {' '.join(buf)}")
        lines.append("")

    pending = list(sus)
    for s in t.segments:
        text = s.text.strip()
        if not text:
            continue
        # 누락 의심 구간에 들어서면 문단을 끊고 표식을 그 자리에 둔다
        while pending and pending[0]["start"] < s.end:
            g = pending.pop(0)
            flush()
            cur_spk, buf = object(), []
            lines.append(f"> ⚠ 전사 누락 의심 [{fmt_ts(g['start'])}-{fmt_ts(g['end'])}] {gaps.note(g)}")
            lines.append("")
        # 화자 정보가 없으면(--no-diarize) 세그먼트마다 끊어 타임스탬프를 살린다
        if s.speaker != cur_spk or s.speaker is None:
            flush()
            cur_spk, cur_start, buf = s.speaker, s.start, []
        buf.append(text)
    flush()
    return "\n".join(lines)
