"""rec2md review — 녹취록 검토 화면(클릭 재생, 화자 지정)과 판정 모드.

  rec2md review <이름>.segments.json [--audio <원본>] [--series <이름>] [--summarize-cmd "claude -p"] [--port 0] [--no-open]
  rec2md review --queue <이름>.cases.json [--audio <원본>]
  rec2md review --print-guide        # 요약 지침 전문(summarize.GUIDE)

표준 라이브러리 http.server로 127.0.0.1에만 띄운다. 페이지는 단일 HTML(외부 CDN 없음)이고, 오디오는
HTTP Range(206)로 보내 탐색이 된다. 편집은 한 건마다 저장된다(review.Session). Ctrl-C로 끝낸다.
옆 파일(요약 md, 메모, corrections.json)은 review.py 첫머리 참고.
요약 탭의 "요약 만들기"는 summarize.Job이 외부 명령을 별도 스레드로 돌린다(POST로 시작, GET으로 상태 폴링).
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import mimetypes
import re
import sys
import tempfile
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

from . import summarize
from .review import MemoConflict, Queue, Session

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

    def __init__(self, session: Session | None, queue: Queue | None, audio: Path | None, series: str | None = None,
                 summarize_cmd: str | None = None):
        self.session, self.queue, self.audio, self.series = session, queue, audio, series
        self.lock = threading.Lock()
        self.job = (summarize.Job(session.seg, session.stem, summarize.resolve_cmd(summarize_cmd))
                    if session else None)

    def start_summary(self) -> None:
        with self.lock:  # 편집 중간 상태를 집지 않게, 지금 화면 상태를 복사해 넘긴다
            t, title = copy.deepcopy(self.session.current), self.session.title()
        self.job.start(t, title)

    def state(self) -> dict:
        st = self.session.state() if self.session else self.queue.state()
        st["audio"] = bool(self.audio)
        return st


LOOPBACK = re.compile(r"(127\.0\.0\.1|localhost|\[::1\])(:\d+)?")


def loopback(hostport: str) -> bool:
    """'127.0.0.1:9000', 'localhost', '[::1]:8080' → True. 포트는 숫자이기만 하면 무엇이든."""
    return LOOPBACK.fullmatch(hostport.strip().lower()) is not None


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

        def _same_origin(self, post: bool) -> str | None:
            """다른 웹페이지가 이 서버를 부르지 못하게 한다. 문제가 있으면 거절 이유, 괜찮으면 None.
            - Host: 호스트 이름이 127.0.0.1·localhost·[::1]일 때만(DNS 리바인딩으로 남의 도메인이 이 서버를
              가리키는 것 차단). 포트는 보지 않는다: `ssh -L 9000:127.0.0.1:<포트>`로 다른 머신에서 열면 포트가 바뀐다
            - Origin: 있으면 같은 기준(http://<위 호스트>[:포트])
            - POST는 Content-Type: application/json만(폼·text/plain 단순 요청은 프리플라이트 없이 오므로 막는다.
              JSON 요청은 프리플라이트가 붙고 이 서버는 CORS를 허락하지 않으므로 다른 출처에서는 못 보낸다)"""
            if not loopback(self.headers.get("Host") or ""):
                return "Host가 이 서버가 아님"
            origin = self.headers.get("Origin")
            if origin is not None and not (origin.lower().startswith("http://") and loopback(origin[7:])):
                return "다른 출처의 요청"
            if post and (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                return "Content-Type은 application/json만"
            return None

        def do_GET(self):
            if why := self._same_origin(post=False):
                self._json({"error": why}, HTTPStatus.FORBIDDEN)
                return
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
            elif app.job and path == "/api/summarize":
                self._json(app.job.status())
            elif app.session and path in ("/api/copy", "/api/panel", "/api/keywords"):
                with app.lock:
                    if path == "/api/copy":
                        self._json({"text": app.session.transcript_txt()})
                    elif path == "/api/panel":
                        self._json(app.session.panel())
                    else:
                        self._json({"keywords": app.session.keywords(app.series)})
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
            if why := self._same_origin(post=True):
                self._json({"error": why}, HTTPStatus.FORBIDDEN)
                return
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
            if path == "/api/summarize" and app.job:
                try:
                    app.start_summary()
                except summarize.Busy as e:
                    self._json({"error": str(e)}, HTTPStatus.CONFLICT)
                    return
                except summarize.SummarizeError as e:
                    self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
                    return
                self._json(app.job.status(), HTTPStatus.ACCEPTED)
                return
            try:
                with app.lock:
                    if path == "/api/edit" and app.session:
                        app.session.add(body)
                    elif path == "/api/undo" and app.session:
                        app.session.undo()
                    elif path == "/api/memo" and app.session:
                        self._json({"memo": app.session.save_memo(body.get("text"), body.get("base"))})
                        return
                    elif path == "/api/answer" and app.queue:
                        app.queue.answer(body["id"], body.get("key"), body.get("text"), bool(body.get("unsure")))
                    else:
                        self._json({"error": "없는 API"}, HTTPStatus.NOT_FOUND)
                        return
                    self._json(app.state())
            except MemoConflict as e:
                self._json({"error": str(e)}, HTTPStatus.CONFLICT)
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


def playable_audio(audio: Path, cache_dir: Path | None = None) -> Path:
    """브라우저가 못 여는 코덱이면 재생용 mp3 사본을 임시 폴더에 만들어 그것을 준다.

    사본은 원본 옆이 아니라 임시 폴더에 둔다(원본이 녹음기 같은 외부 장치일 수 있음).
    이름에 원본 경로·크기·수정시각 해시를 넣어 같은 파일은 다시 변환하지 않는다.
    """
    from . import audio as audio_mod

    codec = audio_mod.probe_codec(audio)
    if codec is None or codec in audio_mod.BROWSER_CODECS:
        return audio
    st = audio.stat()
    cache = cache_dir or Path(tempfile.gettempdir()) / "rec2md-play"
    cache.mkdir(parents=True, exist_ok=True)
    # 녹음기는 같은 RECnnnnn 번호를 다시 쓰므로 이름·크기만으로는 겹칠 수 있다 → 절대경로 해시와 ns 수정시각을 넣는다
    key = hashlib.sha1(f"{audio.resolve()}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:12]
    dst = cache / f"{audio.stem}-{key}.mp3"
    if not dst.is_file():
        log(f"{audio.name}: 브라우저가 재생 못 하는 코덱({codec}) → 재생용 mp3 만드는 중")
        audio_mod.to_mp3(audio, dst)
    return dst


def serve(app: App, port: int = 0) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="rec2md review", description="녹취록 검토 화면 (클릭 재생, 화자 지정) / 판정 모드")
    p.add_argument("segments", nargs="?", type=Path, help="<이름>.segments.json")
    p.add_argument("--queue", type=Path, metavar="CASES", help="판정 모드: <이름>.cases.json")
    p.add_argument("--audio", type=Path, help="원본 오디오 (기본: segments의 source, cases의 audio를 옆에서 찾음)")
    p.add_argument("--series", help="키워드 칩에 쓸 시리즈 용어집 (기본: corrections.json에 적힌 시리즈)")
    p.add_argument("--summarize-cmd", metavar="CMD",
                   help=f"요약 탭 '요약 만들기'에 쓸 명령. 프롬프트를 stdin으로 받아 md를 stdout으로 내는 것 "
                        f"(기본: ${summarize.ENV_CMD} 또는 '{summarize.DEFAULT_CMD}')")
    p.add_argument("--print-guide", action="store_true", help="요약 지침 전문을 출력하고 끝냄")
    p.add_argument("--port", type=int, default=0, help="포트 (기본 0: 빈 포트)")
    p.add_argument("--no-open", action="store_true", help="브라우저를 열지 않음")
    args = p.parse_args(argv)

    if args.print_guide:
        print(summarize.GUIDE, end="")
        return
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
    else:
        audio = playable_audio(audio)

    httpd = serve(App(session, queue, audio, args.series, args.summarize_cmd), args.port)
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
