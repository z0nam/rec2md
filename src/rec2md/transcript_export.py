"""클로바노트식 녹취록 내보내기 (txt, md, html).

클로바노트 txt 배치(제목, 일시·길이, 참석자 줄, 그 아래 "화자 mm:ss" + 발화)를
따르며 clova_txt 파서로 다시 읽었을 때 왕복(round-trip)이 성립한다.
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from .segments import Transcript


def _format_datetime(t: Transcript) -> str:
    if t.meta.get("datetime"):
        return str(t.meta["datetime"])
    if t.meta.get("date"):
        d = str(t.meta["date"])
        if t.meta.get("time"):
            return f"{d} {t.meta['time']}"
        return d
    now = datetime.now()
    day = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][now.weekday()]
    ampm = "AM" if now.hour < 12 else "PM"
    h12 = now.hour % 12 or 12
    return f"{now.strftime('%Y.%m.%d')} {day} {ampm} {h12}:{now.strftime('%M')}"


def format_duration(dur_sec: float) -> str:
    """초 단위를 클로바노트 형식(Hours Minutes seconds) 문자열로 변환."""
    tot = max(0, int(round(dur_sec)))
    h = tot // 3600
    m = (tot % 3600) // 60
    s = tot % 60
    if h > 0:
        return f"{h}Hours {m}Minutes {s}seconds"
    return f"{m}Minutes {s}seconds"


def format_ts(sec: float) -> str:
    """초 단위를 클로바노트 타임스탬프(mm:ss 또는 hh:mm:ss)로 변환."""
    tot = max(0, int(round(sec)))
    h = tot // 3600
    m = (tot % 3600) // 60
    s = tot % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def resolve_speaker(spk: str | None, names: dict[str, str] | None, meta: dict) -> str:
    """화자 라벨(예: 화자1)을 실명 매핑(names 또는 meta.speaker_names)으로 변환."""
    if not spk:
        return "화자"
    if names and spk in names:
        val = names[spk]
        return val if isinstance(val, str) else val.get("name", spk)
    spk_names = meta.get("speaker_names")
    if isinstance(spk_names, dict) and spk in spk_names:
        val = spk_names[spk]
        if isinstance(val, dict):
            return val.get("name") or spk
        if isinstance(val, str):
            return val
    return spk


def render_clova_txt(t: Transcript, names: dict[str, str] | None = None) -> str:
    """클로바노트 txt 내보내기 형식 생성."""
    title = t.meta.get("title") or Path(t.source).stem or "녹취록"
    dt_str = _format_datetime(t)
    dur_str = format_duration(t.duration)

    unique_speakers = list(dict.fromkeys(
        resolve_speaker(s.speaker, names, t.meta)
        for s in t.segments
        if s.text.strip()
    ))
    attendees_str = " ".join(unique_speakers)

    lines = [
        title,
        f"{dt_str} ・ {dur_str}",
        attendees_str,
        "",
    ]
    for s in t.segments:
        text = s.text.strip()
        if not text:
            continue
        spk = resolve_speaker(s.speaker, names, t.meta)
        lines.append(f"{spk} {format_ts(s.start)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def render_clova_md(t: Transcript, names: dict[str, str] | None = None) -> str:
    """클로바노트식 Markdown 생성."""
    title = t.meta.get("title") or Path(t.source).stem or "녹취록"
    dt_str = _format_datetime(t)
    dur_str = format_duration(t.duration)

    unique_speakers = list(dict.fromkeys(
        resolve_speaker(s.speaker, names, t.meta)
        for s in t.segments
        if s.text.strip()
    ))
    attendees_str = ", ".join(unique_speakers) if unique_speakers else "없음"

    lines = [
        f"# {title}",
        "",
        f"{dt_str} ・ {dur_str}  ",
        f"**참석자**: {attendees_str}",
        "",
        "---",
        "",
    ]
    for s in t.segments:
        text = s.text.strip()
        if not text:
            continue
        spk = resolve_speaker(s.speaker, names, t.meta)
        lines.append(f"**{spk}** `{format_ts(s.start)}`")
        lines.append("")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def render_clova_html(t: Transcript, names: dict[str, str] | None = None) -> str:
    """클로바노트 스타일 단독 HTML 생성 (인쇄 CSS 포함)."""
    title = t.meta.get("title") or Path(t.source).stem or "녹취록"
    dt_str = _format_datetime(t)
    dur_str = format_duration(t.duration)

    unique_speakers = list(dict.fromkeys(
        resolve_speaker(s.speaker, names, t.meta)
        for s in t.segments
        if s.text.strip()
    ))

    attendees_html = "".join(
        f'<span class="attendee-tag">{html.escape(spk)}</span>'
        for spk in unique_speakers
    )

    turns_html_parts = []
    for s in t.segments:
        text = s.text.strip()
        if not text:
            continue
        spk = resolve_speaker(s.speaker, names, t.meta)
        ts = format_ts(s.start)
        escaped_text = html.escape(text)
        turns_html_parts.append(
            f'<div class="turn">\n'
            f'  <div class="turn-header">\n'
            f'    <span class="speaker-name">{html.escape(spk)}</span>\n'
            f'    <span class="timestamp">{ts}</span>\n'
            f'  </div>\n'
            f'  <p class="utterance">{escaped_text}</p>\n'
            f'</div>'
        )
    turns_html = "\n".join(turns_html_parts)

    escaped_title = html.escape(title)
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
    <div class="meta">{html.escape(dt_str)} ・ {html.escape(dur_str)}</div>
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
) -> Path:
    """Transcript를 지정 형식(txt, md, html)으로 내보낸다.

    Args:
        t: 녹취 데이터 (Transcript)
        fmt: 내보낼 형식 ('txt', 'md', 'html')
        out: 출력 파일 경로 또는 디렉터리 경로
        names: 화자 라벨 매핑 (예: {"화자1": "홍길동"}) - 미지정시 t.meta["speaker_names"] 참조

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
        content = render_clova_txt(t, names=names)
    elif fmt == "md":
        content = render_clova_md(t, names=names)
    elif fmt == "html":
        content = render_clova_html(t, names=names)
    else:
        raise ValueError(f"지원하지 않는 형식입니다: {fmt}")

    target.write_text(content, encoding="utf-8")
    return target
