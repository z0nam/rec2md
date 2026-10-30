"""export.html_to_pdf: Chrome의 종료 코드가 아니라 결과 pdf로 성공을 판단한다.

부하가 걸리면 Chrome이 pdf를 다 쓴 뒤 "Teardown watchdog expired"로 0이 아닌 코드를 내는데,
예전에는 이걸 실패로 봐서 테스트가 가끔 깨졌다. 가짜 Chrome 스크립트로 재현한다.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from rec2md import export

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def fake_chrome(tmp_path: Path, body: str) -> Path:
    """--print-to-pdf=<경로>를 받아 body대로 행동하는 가짜 Chrome. 호출 횟수를 calls 파일에 남긴다."""
    p = tmp_path / "fake-chrome"
    p.write_text(f"""#!/bin/sh
echo x >> "{tmp_path}/calls"
for a in "$@"; do case "$a" in --print-to-pdf=*) OUT="${{a#--print-to-pdf=}}";; esac; done
{body}
""", encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def calls(tmp_path: Path) -> int:
    f = tmp_path / "calls"
    return len(f.read_text().split()) if f.exists() else 0


@pytest.fixture
def html(tmp_path) -> Path:
    p = tmp_path / "m.html"
    p.write_text("<p>x</p>", encoding="utf-8")
    return p


pytestmark = pytest.mark.skipif(os.name == "nt", reason="가짜 Chrome이 sh 스크립트")


def test_complete_pdf_with_nonzero_exit_is_success(tmp_path, html, monkeypatch):
    pdf = tmp_path / "src.pdf"
    pdf.write_bytes(PDF)
    monkeypatch.setenv("REC2MD_CHROME", str(fake_chrome(tmp_path, f'cp "{pdf}" "$OUT"; echo "Teardown watchdog expired" >&2; exit 1')))
    out = export.html_to_pdf(html, tmp_path / "m.pdf")
    assert out.read_bytes() == PDF and calls(tmp_path) == 1


def test_truncated_pdf_is_retried(tmp_path, html, monkeypatch):
    """첫 시도는 덜 쓴 pdf, 두 번째는 완성본 → 성공."""
    pdf = tmp_path / "src.pdf"
    pdf.write_bytes(PDF)
    body = f'''if [ -f "{tmp_path}/second" ]; then cp "{pdf}" "$OUT"; else printf '%%PDF-1.4 trunc' > "$OUT"; touch "{tmp_path}/second"; exit 1; fi'''
    monkeypatch.setenv("REC2MD_CHROME", str(fake_chrome(tmp_path, body)))
    assert export.html_to_pdf(html, tmp_path / "m.pdf").read_bytes() == PDF
    assert calls(tmp_path) == 2


def test_no_pdf_fails_after_retries(tmp_path, html, monkeypatch):
    monkeypatch.setenv("REC2MD_CHROME", str(fake_chrome(tmp_path, 'echo boom >&2; exit 3')))
    with pytest.raises(export.ExportError, match="2회 시도.*boom"):
        export.html_to_pdf(html, tmp_path / "m.pdf")
    assert calls(tmp_path) == 2 and not (tmp_path / "m.pdf").exists()


def test_pdf_complete(tmp_path):
    p = tmp_path / "a.pdf"
    p.write_bytes(PDF)
    assert export._pdf_complete(p)
    p.write_bytes(PDF[:20])
    assert not export._pdf_complete(p)
    assert not export._pdf_complete(tmp_path / "none.pdf")
