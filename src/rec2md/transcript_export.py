"""클로바노트식 녹취록 내보내기 (txt, md, html).

클로바노트 txt 배치(제목, 일시·길이, 참석자 줄, 그 아래 "화자 MM:SS" 또는 "화자 H:MM:SS" + 발화)를
따르며 clova_txt 파서로 다시 읽었을 때 왕복(round-trip)이 성립한다.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path

from .segments import Transcript


@dataclass
class Turn:
    speaker: str
    start: float
    text: str


def _format_datetime(t: Transcript, recorded_at: str | None = None) -> str:
    if recorded_at:
        return recorded_at
    if t.meta.get("datetime"):
        return str(t.meta["datetime"])
    if t.meta.get("date"):
        d = str(t.meta["date"])
        if t.meta.get("time"):
            return f"{d} {t.meta['time']}"
        return d
    # 녹음 일시를 모르면 비워 둔다 — 내보낸 시각을 녹음 일시처럼 적으면 틀린 정보가 된다
    return ""


def _head_line(dt_str: str, dur_str: str) -> str:
    """"일시 ・ 길이". 일시가 없으면 "・ 길이"(clova_txt 파서는 ・ 뒤만 읽는다)."""
    return f"{dt_str} ・ {dur_str}" if dt_str else f"・ {dur_str}"


def format_duration(dur_sec: float) -> str:
    """초 단위를 클로바노트 파서가 인식하는 지속시간 문자열로 변환."""
    tot = max(0, int(round(dur_sec)))
    h = tot // 3600
    m = (tot % 3600) // 60
    s = tot % 60
    if h > 0:
        return f"{h}Hours {m}Minutes {s}seconds"
    return f"{m}Minutes {s}seconds"


def format_ts(sec: float) -> str:
    """초 단위를 클로바 타임스탬프 (1시간 미만 MM:SS, 이상 H:MM:SS)로 변환."""
    tot = max(0, int(round(sec)))
    h = tot // 3600
    m = (tot % 3600) // 60
    s = tot % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def resolve_speaker(spk: str | None, names: dict[str, str] | None, meta: dict) -> str:
    """화자 라벨(예: 화자1)을 실명 매핑으로 변환. 없으면 '참석자 N'.

    목소리 DB가 자동으로 붙인 이름 중 확신이 낮은 것(tentative)은 `이름?`으로 표시한다.
    사용자가 지정한 이름(source "user")과 `names` 인자로 준 이름은 그대로 쓴다.
    """
    if not spk:
        return "참석자 1"
    if names and spk in names:
        val = names[spk]
        return val if isinstance(val, str) else val.get("name", spk)
    spk_names = meta.get("speaker_names")
    if isinstance(spk_names, dict) and spk in spk_names:
        val = spk_names[spk]
        if isinstance(val, dict):
            name = val.get("name")
            if not name:
                return spk
            return f"{name}?" if val.get("tentative") and val.get("source") != "user" else name
        if isinstance(val, str):
            return val

    # 화자N -> 참석자 N 변환
    m = re.match(r"^화자\s*(\d+)$", spk)
    if m:
        return f"참석자 {m.group(1)}"
    m2 = re.match(r"^SPEAKER_(\d+)$", spk, re.I)
    if m2:
        return f"참석자 {int(m2.group(1)) + 1}"
    return spk


def attendees(turns: list[Turn]) -> list[str]:
    """참석자 목록(처음 말한 순서). 한 사람이 여러 화자로 쪼개져(same_as) `이름`과 `이름?`으로 나와도
    한 번만 적고, 확신 있는 표기(`?` 없음)가 하나라도 있으면 그것을 쓴다."""
    out: dict[str, str] = {}
    for turn in turns:
        key = turn.speaker.rstrip("?")
        if out.get(key, "?").endswith("?"):
            out[key] = turn.speaker
    return list(out.values())


def iter_turns(t: Transcript, names: dict[str, str] | None = None) -> list[Turn]:
    """같은 화자가 이어 말한 세그먼트는 한 블록으로 합친다 (render.render_raw 규칙)."""
    turns: list[Turn] = []
    cur_spk_raw, cur_spk_name, cur_start, buf = object(), "", 0.0, []

    def flush():
        if buf:
            turns.append(Turn(speaker=cur_spk_name, start=cur_start, text=" ".join(buf)))

    for s in t.segments:
        text = s.text.strip()
        if not text:
            continue
        # 화자 정보가 없으면 세그먼트마다 끊어 타임스탬프를 살린다
        if s.speaker != cur_spk_raw or s.speaker is None:
            flush()
            cur_spk_raw = s.speaker
            cur_spk_name = resolve_speaker(s.speaker, names, t.meta)
            cur_start = s.start
            buf = []
        buf.append(text)
    flush()
    return turns


def render_clova_txt(
    t: Transcript,
    names: dict[str, str] | None = None,
    title: str | None = None,
    recorded_at: str | None = None,
) -> str:
    """클로바노트 txt 내보내기 형식 생성."""
    doc_title = title or t.meta.get("title") or Path(t.source).stem or "녹취록"
    dt_str = _format_datetime(t, recorded_at=recorded_at)
    dur_str = format_duration(t.duration)

    turns = iter_turns(t, names=names)
    unique_speakers = attendees(turns)
    attendees_str = " ".join(unique_speakers)

    lines = [
        doc_title,
        _head_line(dt_str, dur_str),
        attendees_str,
        "",
    ]
    for turn in turns:
        lines.append(f"{turn.speaker} {format_ts(turn.start)}")
        for subline in turn.text.splitlines():
            sline = subline.strip()
            if sline:
                lines.append(sline)
        lines.append("")
    return "\n".join(lines)


def render_clova_md(
    t: Transcript,
    names: dict[str, str] | None = None,
    title: str | None = None,
    recorded_at: str | None = None,
) -> str:
    """클로바노트식 Markdown 생성."""
    doc_title = title or t.meta.get("title") or Path(t.source).stem or "녹취록"
    dt_str = _format_datetime(t, recorded_at=recorded_at)
    dur_str = format_duration(t.duration)

    turns = iter_turns(t, names=names)
    unique_speakers = attendees(turns)
    attendees_str = ", ".join(unique_speakers) if unique_speakers else "없음"

    lines = [
        f"# {doc_title}",
        "",
        f"{_head_line(dt_str, dur_str)}  ",
        f"**참석자**: {attendees_str}",
        "",
        "---",
        "",
    ]
    for turn in turns:
        lines.append(f"**{turn.speaker}** {format_ts(turn.start)}")
        lines.append(turn.text)
        lines.append("")
    return "\n".join(lines)


def render_clova_html(
    t: Transcript,
    names: dict[str, str] | None = None,
    title: str | None = None,
    recorded_at: str | None = None,
) -> str:
    """클로바노트 스타일 단독 HTML 생성 (인쇄 친화 인라인 CSS 포함)."""
    doc_title = title or t.meta.get("title") or Path(t.source).stem or "녹취록"
    dt_str = _format_datetime(t, recorded_at=recorded_at)
    dur_str = format_duration(t.duration)

    turns = iter_turns(t, names=names)
    unique_speakers = attendees(turns)

    attendees_html = "".join(
        f'<span class="attendee-tag">{html.escape(spk)}</span>'
        for spk in unique_speakers
    )

    turns_html_parts = []
    for turn in turns:
        ts = format_ts(turn.start)
        escaped_text = html.escape(turn.text)
        turns_html_parts.append(
            f'<div class="turn">\n'
            f'  <div class="turn-header">\n'
            f'    <span class="speaker-name">{html.escape(turn.speaker)}</span>\n'
            f'    <span class="timestamp">{ts}</span>\n'
            f'  </div>\n'
            f'  <p class="utterance">{escaped_text}</p>\n'
            f'</div>'
        )
    turns_html = "\n".join(turns_html_parts)

    escaped_title = html.escape(doc_title)
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escaped_title}</title>
<style>
  :root {{
    --bg: #ffffff;
    --text: #191f28;
    --text-muted: #8b95a1;
    --border: #e5e8eb;
    --card-bg: #f9fafb;
    --primary: #2563eb;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #1a1c20;
      --text: #f2f4f6;
      --text-muted: #8b95a1;
      --border: #333d4b;
      --card-bg: #22262c;
      --primary: #60a5fa;
    }}
  }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Pretendard", "Noto Sans KR", sans-serif;
    line-height: 1.6;
    color: var(--text);
    background: var(--bg);
    margin: 0;
    padding: 32px 16px;
  }}
  .container {{
    max-width: 800px;
    margin: 0 auto;
  }}
  header {{
    border-bottom: 2px solid var(--border);
    padding-bottom: 20px;
    margin-bottom: 24px;
  }}
  h1 {{
    font-size: 1.6rem;
    margin: 0 0 12px 0;
    line-height: 1.3;
  }}
  .meta {{
    font-size: 0.95rem;
    color: var(--text-muted);
    margin-bottom: 8px;
  }}
  .attendees {{
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 10px;
    font-size: 0.9rem;
    align-items: center;
  }}
  .attendee-label {{
    color: var(--text-muted);
    font-weight: 500;
    margin-right: 4px;
  }}
  .attendee-tag {{
    background: var(--card-bg);
    border: 1px solid var(--border);
    padding: 2px 10px;
    border-radius: 12px;
  }}
  .turns {{
    display: flex;
    flex-direction: column;
    gap: 20px;
  }}
  .turn {{
    display: flex;
    flex-direction: column;
    gap: 4px;
  }}
  .turn-header {{
    display: flex;
    align-items: baseline;
    gap: 8px;
  }}
  .speaker-name {{
    font-weight: 700;
    font-size: 1rem;
    color: var(--primary);
  }}
  .timestamp {{
    font-size: 0.85rem;
    color: var(--text-muted);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }}
  .utterance {{
    margin: 0;
    font-size: 1rem;
    white-space: pre-wrap;
    word-break: break-word;
  }}
  @page {{
    size: A4;
    margin: 20mm;
  }}
  @media print {{
    body {{ background: white; color: black; padding: 0; }}
    .turn {{ break-inside: avoid; }}
  }}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>{escaped_title}</h1>
    <div class="meta">{html.escape(_head_line(dt_str, dur_str))}</div>
    <div class="attendees">
      <span class="attendee-label">참석자:</span>
      {attendees_html}
    </div>
  </header>
  <div class="turns">
{turns_html}
  </div>
</div>
</body>
</html>
"""


def export_transcript(
    t: Transcript,
    fmt: str,
    out: Path,
    names: dict[str, str] | None = None,
    title: str | None = None,
    recorded_at: str | None = None,
) -> Path:
    """Transcript를 지정 형식(txt, md, html)으로 내보낸다.

    Args:
        t: 녹취 데이터 (Transcript)
        fmt: 내보낼 형식 ('txt', 'md', 'html')
        out: 출력 파일 경로 또는 디렉터리 경로
        names: 화자 라벨 매핑 (예: {"화자1": "홍길동"}) - 미지정시 t.meta["speaker_names"] 참조
        title: 회의명 (미지정시 t.meta["title"] 또는 파일명)
        recorded_at: 녹음 일시 문자열 (미지정시 t.meta 일시, 그것도 없으면 비움)

    Returns:
        실제 저장된 파일의 Path
    """
    fmt = fmt.lstrip(".").lower()
    if fmt not in ("txt", "md", "html"):
        raise ValueError(f"지원하지 않는 형식입니다: {fmt} (지원: txt, md, html)")

    target: Path
    if out.is_dir() or (not out.suffix and not out.exists()):
        stem = Path(t.source).stem if t.source else "transcript"
        target = out / f"{stem}.{fmt}"
    else:
        target = out

    target.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "txt":
        content = render_clova_txt(t, names=names, title=title, recorded_at=recorded_at)
    elif fmt == "md":
        content = render_clova_md(t, names=names, title=title, recorded_at=recorded_at)
    elif fmt == "html":
        content = render_clova_html(t, names=names, title=title, recorded_at=recorded_at)
    else:
        raise ValueError(f"지원하지 않는 형식입니다: {fmt}")

    target.write_text(content, encoding="utf-8")
    return target
