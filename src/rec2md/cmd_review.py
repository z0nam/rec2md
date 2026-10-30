"""rec2md review — 녹취록 검토 화면(클릭 재생, 화자 지정)과 판정 모드.

  rec2md review <이름>.segments.json [--audio <원본>] [--port 0] [--no-open]
  rec2md review --queue <이름>.cases.json [--audio <원본>]

표준 라이브러리 http.server로 127.0.0.1에만 띄운다. 페이지는 단일 HTML(외부 CDN 없음)이고, 오디오는
HTTP Range(206)로 보내 탐색이 된다. 편집은 한 건마다 저장된다(review.Session). Ctrl-C로 끝낸다.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

from .review import Queue, Session

MAX_BODY = 1 << 20
CHUNK = 1 << 16


def log(msg: str) -> None:
    print(f"[rec2md] {msg}", file=sys.stderr, flush=True)


def page() -> bytes:
    return resources.files("rec2md").joinpath("review_page.html").read_bytes()


def parse_range(header: str, size: int) -> tuple[int, int] | None:
    """'bytes=a-b' → (a, b) 포함 구간. 형식이 틀리면 None(전체 전송), 만족할 수 없으면 ValueError(416)."""
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not m or (not m.group(1) and not m.group(2)):
        return None
    a, b = m.groups()
    if not a:  # 끝에서 n바이트
        n = int(b)
        if n == 0:
            raise ValueError
        return max(0, size - n), size - 1
    start = int(a)
    end = min(int(b), size - 1) if b else size - 1
    if start >= size or start > end:
        raise ValueError
    return start, end


class App:
    """요청 처리기에 넘길 상태: 검토 세션 또는 판정 큐, 그리고 오디오 경로."""

    def __init__(self, session: Session | None, queue: Queue | None, audio: Path | None):
        self.session, self.queue, self.audio = session, queue, audio
        self.lock = threading.Lock()

    def state(self) -> dict:
        st = self.session.state() if self.session else self.queue.state()
        st["audio"] = bool(self.audio)
        return st


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "rec2md-review"

        def log_message(self, fmt, *args):  # 요청마다 찍지 않는다
            pass

        def _json(self, obj, status=HTTPStatus.OK):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                body = page()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/state":
                with app.lock:
                    self._json(app.state())
            elif path == "/audio":
                self._audio()
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def _audio(self):
            if not app.audio or not app.audio.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "오디오 없음")
                return
            size = app.audio.stat().st_size
            ctype = mimetypes.guess_type(app.audio.name)[0] or "application/octet-stream"
            try:
                rng = parse_range(self.headers.get("Range", ""), size) if self.headers.get("Range") else None
            except ValueError:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = rng if rng else (0, size - 1)
            self.send_response(HTTPStatus.PARTIAL_CONTENT if rng else HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            try:
                with app.audio.open("rb") as f:
                    f.seek(start)
                    left = end - start + 1
                    while left > 0:
                        buf = f.read(min(CHUNK, left))
                        if not buf:
                            break
                        self.wfile.write(buf)
                        left -= len(buf)
            except (BrokenPipeError, ConnectionResetError):  # 브라우저가 탐색하며 끊는 것은 정상
                pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                self._json({"error": "요청이 너무 큼"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                self._json({"error": "JSON이 아님"}, HTTPStatus.BAD_REQUEST)
                return
            path = self.path.split("?", 1)[0]
            try:
                with app.lock:
                    if path == "/api/edit" and app.session:
                        app.session.add(body)
                    elif path == "/api/undo" and app.session:
                        app.session.undo()
                    elif path == "/api/answer" and app.queue:
                        app.queue.answer(body["id"], body.get("key"), body.get("text"), bool(body.get("unsure")))
                    else:
                        self._json({"error": "없는 API"}, HTTPStatus.NOT_FOUND)
                        return
                    self._json(app.state())
            except (ValueError, KeyError, TypeError) as e:
                self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)

    return Handler


def find_audio(base: Path, name: str | None) -> Path | None:
    if not name:
        return None
    p = Path(name)
    for c in ([p] if p.is_absolute() else [base / p, base / p.name]):
        if c.is_file():
            return c
    return None


def serve(app: App, port: int = 0) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="rec2md review", description="녹취록 검토 화면 (클릭 재생, 화자 지정) / 판정 모드")
    p.add_argument("segments", nargs="?", type=Path, help="<이름>.segments.json")
    p.add_argument("--queue", type=Path, metavar="CASES", help="판정 모드: <이름>.cases.json")
    p.add_argument("--audio", type=Path, help="원본 오디오 (기본: segments의 source, cases의 audio를 옆에서 찾음)")
    p.add_argument("--port", type=int, default=0, help="포트 (기본 0: 빈 포트)")
    p.add_argument("--no-open", action="store_true", help="브라우저를 열지 않음")
    args = p.parse_args(argv)

    if bool(args.segments) == bool(args.queue):
        raise SystemExit("segments.json 하나 또는 --queue <cases.json> 중 하나만 줄 것")
    session = queue = None
    if args.queue:
        if not args.queue.is_file():
            raise SystemExit(f"파일 없음: {args.queue}")
        try:
            queue = Queue(args.queue)
        except ValueError as e:
            raise SystemExit(str(e)) from None
        audio = args.audio or find_audio(args.queue.parent, queue.data.get("audio"))
    else:
        if not args.segments.is_file():
            raise SystemExit(f"파일 없음: {args.segments}")
        session = Session(args.segments)
        if session.notice:
            log(session.notice)
        audio = args.audio or find_audio(args.segments.parent, session.current.source)
    if audio is None:
        log("오디오를 찾지 못함 — 재생 없이 연다 (--audio로 지정)")
    elif not audio.is_file():
        raise SystemExit(f"오디오 없음: {audio}")

    httpd = serve(App(session, queue, audio), args.port)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    log(f"{url} (Ctrl-C로 끝냄)")
    if not args.no_open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("끝")
    finally:
        httpd.server_close()
