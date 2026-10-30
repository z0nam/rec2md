"""렌더 결과 → pdf·png.

html은 여기서 직접 바꾼다(헤드리스 Chrome, 없으면 WeasyPrint). 다른 형식(docx, hwpx …)은
`rec2md.render.<형식>` 모듈의 `to_pdf(src, out) -> Path`, 선택 `to_png(src, out_dir) -> list[Path]`를
찾아 부른다. 형식별 변환 코드는 그 렌더러 모듈이 가진다 (docs/templates.md).
png는 쪽마다 한 장: `<이름>.p1.png`, `<이름>.p2.png` …
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
]


class ExportError(RuntimeError):
    """변환 도구가 없거나 실패. 메시지에 설치 안내를 담는다."""


def find_chrome() -> str | None:
    if env := os.environ.get("REC2MD_CHROME"):
        return env
    for c in CHROME_CANDIDATES:
        if os.path.isabs(c) and os.path.exists(c):
            return c
        if found := shutil.which(c):
            return found
    return None


def _pdf_complete(p: Path) -> bool:
    """끝까지 쓰인 pdf인가: %PDF-로 시작하고 끝 부분에 %%EOF가 있다."""
    try:
        data = p.read_bytes()
    except OSError:
        return False
    return data.startswith(b"%PDF-") and b"%%EOF" in data[-1024:]


def html_to_pdf(src: Path, out: Path, attempts: int = 2) -> Path:
    if chrome := find_chrome():
        err = ""
        for _ in range(attempts):
            out.unlink(missing_ok=True)
            with tempfile.TemporaryDirectory(prefix="rec2md-chrome-") as profile:
                r = subprocess.run([chrome, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                                    "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                                    f"--user-data-dir={profile}", f"--print-to-pdf={out.resolve()}",
                                    src.resolve().as_uri()], capture_output=True, text=True, timeout=120)
            # 부하가 걸리면 Chrome이 pdf를 다 쓴 뒤 종료 정리에서 "Teardown watchdog expired"로 0이 아닌
            # 코드를 낸다. 종료 코드가 아니라 결과 파일로 판단하고, 파일이 덜 됐을 때만 다시 시도한다
            if _pdf_complete(out):
                return out
            err = f"종료 코드 {r.returncode}: {r.stderr.strip()[-500:]}"
        raise ExportError(f"Chrome pdf 변환 실패 ({attempts}회 시도) — {err}")
    try:
        import weasyprint
    except ModuleNotFoundError:
        raise ExportError("html → pdf에 Chrome(또는 Chromium)이나 WeasyPrint가 필요함: "
                          "Chrome 설치, 경로를 REC2MD_CHROME으로 지정, 또는 pip install weasyprint") from None
    weasyprint.HTML(filename=str(src)).write_pdf(str(out))
    return out


def pdf_to_png(pdf: Path, out_dir: Path, stem: str, scale: float = 2.0) -> list[Path]:
    """pdf 쪽마다 png 한 장. pypdfium2, 없으면 pdftoppm(poppler)."""
    try:
        import pypdfium2 as pdfium
    except ModuleNotFoundError:
        pdfium = None
    if pdfium is not None:
        doc, outs = pdfium.PdfDocument(str(pdf)), []
        try:
            for i, page in enumerate(doc, 1):
                p = out_dir / f"{stem}.p{i}.png"
                page.render(scale=scale).to_pil().save(p)
                outs.append(p)
        finally:
            doc.close()
        return outs
    if shutil.which("pdftoppm"):
        prefix = out_dir / f"{stem}.p"
        subprocess.run(["pdftoppm", "-png", "-r", str(int(72 * scale)), str(pdf), str(prefix)], check=True)
        # pdftoppm은 p-1.png, p-01.png처럼 붙이므로 p1.png로 맞춘다
        outs = []
        for f in sorted(out_dir.glob(f"{stem}.p-*.png"), key=lambda f: int(f.stem.rsplit("-", 1)[1])):
            n = int(f.stem.rsplit("-", 1)[1])
            outs.append(f.rename(out_dir / f"{stem}.p{n}.png"))
        return outs
    raise ExportError("pdf → png에 pypdfium2 또는 pdftoppm이 필요함: uv sync --extra export "
                      "(pip install 'rec2md[export]') 또는 brew install poppler")


def _module(fmt: str):
    try:
        return importlib.import_module(f"rec2md.render.{fmt}")
    except ModuleNotFoundError as e:
        if e.name == f"rec2md.render.{fmt}":
            raise ExportError(f"{fmt} 렌더러가 없음 (rec2md.render.{fmt})") from None
        raise


def to_pdf(src: Path, out: Path | None = None) -> Path:
    fmt = src.suffix.lstrip(".").lower()
    out = out or src.with_name(src.name[: -len(src.suffix)] + ".pdf")
    if fmt == "pdf":
        return src
    if fmt == "html":
        return html_to_pdf(src, out)
    fn = getattr(_module(fmt), "to_pdf", None)
    if fn is None:
        raise ExportError(f"{fmt} → pdf 변환이 아직 없음 (rec2md.render.{fmt}.to_pdf)")
    return fn(src, out)


def to_png(src: Path, out_dir: Path | None = None) -> list[Path]:
    fmt = src.suffix.lstrip(".").lower()
    out_dir = out_dir or src.parent
    stem = src.name[: -len(src.suffix)]
    if fmt not in ("html", "pdf"):
        fn = getattr(_module(fmt), "to_png", None)
        if fn is not None:
            return fn(src, out_dir)
    if fmt == "pdf":
        return pdf_to_png(src, out_dir, stem)
    with tempfile.TemporaryDirectory(prefix="rec2md-export-") as td:
        pdf = to_pdf(src, Path(td) / f"{stem}.pdf")
        return pdf_to_png(pdf, out_dir, stem)


def export(src: Path, targets: list[str], out_dir: Path | None = None, stem: str | None = None) -> list[Path]:
    """src를 targets(pdf, png)로. pdf와 png를 같이 달라면 pdf를 한 번만 만든다.
    stem은 출력 이름(기본: src 이름에서 확장자를 뗀 것). 원천이 여럿이면 호출자가 겹치지 않게 준다."""
    if unknown := set(targets) - {"pdf", "png"}:
        raise ExportError(f"모르는 내보내기 형식: {', '.join(sorted(unknown))} (pdf, png)")
    out_dir = out_dir or src.parent
    stem = stem or src.name[: -len(src.suffix)]
    outs: list[Path] = []
    pdf = None
    if "pdf" in targets:
        pdf = to_pdf(src, out_dir / f"{stem}.pdf")
        outs.append(pdf)
    if "png" in targets:
        has_own_png = src.suffix.lower() not in (".html", ".pdf") and hasattr(_module(src.suffix[1:].lower()), "to_png")
        src_stem = src.name[: -len(src.suffix)]
        if pdf and not has_own_png:
            outs += pdf_to_png(pdf, out_dir, stem)
        elif stem == src_stem:
            outs += to_png(src, out_dir)
        elif has_own_png:  # 형식 모듈은 src 이름으로 쓰므로 <src>.pN.png → <stem>.pN.png로 바꾼다
            for o in to_png(src, out_dir):
                new = o.with_name(stem + o.name[len(src_stem):]) if o.name.startswith(src_stem + ".") else o
                outs.append(o.replace(new) if new != o else o)
        else:  # 이름을 바꿔야 하니 임시 pdf를 거친다
            with tempfile.TemporaryDirectory(prefix="rec2md-export-") as td:
                outs += pdf_to_png(to_pdf(src, Path(td) / f"{stem}.pdf"), out_dir, stem)
    return outs
