"""녹취 요약을 외부 명령(기본: 이 맥에 로그인된 `claude -p`)으로 만든다.

계약: 명령은 **프롬프트를 stdin으로 받아 요약 Markdown을 stdout으로** 낸다. 그 밖의 것은 모른다.
그래서 같은 자리에 다른 에이전트 CLI(`codex exec`)나 로컬 모델 래퍼를 꽂을 수 있다.

명령을 고르는 순서: `--summarize-cmd` 인자 → 환경변수 `REC2MD_SUMMARIZE_CMD`
(`~/.config/rec2md/.env`에 적어 두면 늘 쓴다) → 기본 `claude -p`.
API 키는 받지 않는다. 명령이 자기 로그인(구독)으로 돈다.

요약 지침(`GUIDE`)은 여기 한 곳에 둔다. SKILL.md 2단계 3·4번은 이 문구를 가리키고,
`rec2md review --print-guide`로 전문을 볼 수 있다.

결과 파일 `<이름>.요약.md`
- `.part`에 쓴 뒤 이름을 바꾼다(쓰다 죽어도 반쪽 파일이 남지 않음)
- 이미 있으면 덮어쓰기 전에 `<이름>.요약.prev.md`로 하나 남긴다
- 첫 줄 주석에 입력 지문(`segments=<해시>`)을 적어, 그 뒤 편집이 생겼는지 화면이 알 수 있게 한다
- 에이전트가 손으로 쓴 `<이름>.녹취록.md`는 건드리지 않는다
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from .segments import Transcript
from .transcript_export import format_ts, render_clova_txt

DEFAULT_CMD = "claude -p"
ENV_CMD = "REC2MD_SUMMARIZE_CMD"
TIMEOUT = 900  # 초. 두 시간 녹취도 보통 몇 분 안에 끝난다

GUIDE = """\
아래는 회의(또는 통화·강연) 녹취다. 화자 이름과 시각(mm:ss 또는 h:mm:ss)이 붙어 있다. 이 녹취의 요약을 한국어 Markdown으로 써라.

## 회의 구간
- 녹음기를 늦게 켜거나 안 꺼서 앞뒤에 잡담·통화·이동 소음이 붙어 있을 수 있다.
- 아래 "회의 구간 추정"이 있으면 그 근거 구간을 읽어 확인하고, 맞으면 **그 구간 안의 내용만** 요약한다. 틀렸다고 보면 직접 정한 구간을 쓴다.
- 추정이 없으면 전체를 회의로 보되, 앞뒤에 붙은 회의와 무관한 대화는 직접 판단해 뺀다.

## 형식
- 맨 위에 한 줄: `- 회의 구간: <시작>〜<끝> (근거 한 마디)`
- `## 개요`: 3〜5줄. 어떤 회의인지(주관·참석 기관·형식), 목적, 큰 흐름.
- `## 주요 논의`: 안건·주제별 bullet. 각 bullet 앞에 대략 시각(예: `[12:30]`).
- `## 결정사항`: 명시적으로 합의·결정된 것만. 없으면 "명시적 결정 없음".
- `## 할 일`: 담당·기한이 나오면 함께(`- 자료 송부 — 담당: ○○ / 기한: 다음 주`). 없으면 "명시적 할 일 없음".

## 규칙
- 지어내지 않는다. 자동 전사라 고유명사·숫자가 자주 틀린다. 맥락상 명백한 것만 고치고, 확신 없는 이름·숫자·기관명에는 `[?]`를 붙인다.
- 화자 이름이 `화자N`처럼 번호뿐이면 요약에는 쓰지 말고 역할(예: "발표자", "연구원 측")로 적는다.
- 업무와 무관한 사적 대화(인사·평가·신상)는 내용 없이 `[시작〜끝] 업무 무관`으로만 적는다.
- 분량: 회의 1시간당 대략 15〜30줄. 짧은 회의는 짧게.
- **요약 Markdown만** 출력한다. 앞뒤 인사말·설명·코드펜스를 붙이지 않는다.
"""

MARK = re.compile(r"^<!-- rec2md summarize: (.*?) -->\n?")


class SummarizeError(RuntimeError):
    pass


# ---- 명령 ----

def resolve_cmd(arg: str | None = None) -> str:
    return (arg or os.environ.get(ENV_CMD) or DEFAULT_CMD).strip()


def check_cmd(cmd: str) -> str | None:
    """실행할 수 없으면 그 이유(화면 툴팁에 쓴다), 되면 None."""
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        return f"요약 명령을 읽을 수 없음: {e}"
    if not argv:
        return "요약 명령이 비어 있음"
    if shutil.which(argv[0]) is None:
        return (f"'{argv[0]}' 명령이 PATH에 없음 — 설치·로그인하거나 "
                f"--summarize-cmd / {ENV_CMD}로 다른 명령을 지정")
    return None


def argv(cmd: str) -> list[str]:
    """실행할 argv. 첫 토큰은 절대경로로 바꾼다: 명령은 빈 임시 폴더에서 돌리므로
    `./wrapper` 같은 상대경로가 그대로면 거기서 못 찾는다."""
    a = shlex.split(cmd)
    exe = shutil.which(a[0])
    return [os.path.abspath(exe) if exe else a[0], *a[1:]]


# ---- 입력 ----

def fingerprint(t: Transcript) -> str:
    """요약이 기대는 내용(구간·문장·화자·이름)의 지문. review.same()과 같은 범위."""
    key = ([(s.start, s.end, s.text, s.speaker) for s in t.segments], t.meta.get("speaker_names") or {})
    return hashlib.sha1(json.dumps(key, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def bounds_note(t: Transcript) -> str:
    b = t.meta.get("meeting_bounds") or {}
    if not b or b.get("confidence") in (None, "none"):
        return "회의 구간 추정: 없음(전체를 회의로 보고 앞뒤 잡담만 직접 판단)"
    ev = b.get("evidence")
    ev = "; ".join(map(str, ev)) if isinstance(ev, list) else str(ev or "")
    return (f"회의 구간 추정: {format_ts(float(b.get('start') or 0))}〜{format_ts(float(b.get('end') or t.duration))}"
            f" (신뢰도 {b.get('confidence')}){' — 근거: ' + ev if ev else ''}")


def build_prompt(t: Transcript, title: str) -> str:
    return f"{GUIDE}\n---\n{bounds_note(t)}\n\n{render_clova_txt(t, title=title)}"


# ---- 실행·저장 ----

def run(cmd: str, prompt: str, timeout: float = TIMEOUT) -> str:
    """명령을 돌려 stdout(요약 md)을 돌려준다. 실패하면 stderr 끝부분을 담은 SummarizeError."""
    if why := check_cmd(cmd):
        raise SummarizeError(why)
    # 작업 폴더를 빈 임시 폴더로: 에이전트 CLI가 지금 폴더의 프로젝트 지침·파일을 끌어오지 않게
    with tempfile.TemporaryDirectory(prefix="rec2md-summ-") as cwd:
        try:
            r = subprocess.run(argv(cmd), check=False, input=prompt, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout, cwd=cwd)
        except subprocess.TimeoutExpired:
            raise SummarizeError(f"{timeout:.0f}초 안에 끝나지 않음") from None
        except OSError as e:
            raise SummarizeError(f"실행 실패: {e}") from None
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip()[-800:]
        raise SummarizeError(f"종료 코드 {r.returncode}" + (f": {tail}" if tail else ""))
    out = clean(r.stdout)
    if not out:
        raise SummarizeError("요약이 비어 있음" + (f" (stderr: {r.stderr.strip()[-300:]})" if r.stderr.strip() else ""))
    return out


def clean(text: str) -> str:
    """앞뒤 공백과, 모델이 습관적으로 두른 ```markdown 펜스 하나를 걷는다."""
    s = text.strip()
    m = re.fullmatch(r"```(?:markdown|md)?\n(.*)\n```", s, re.DOTALL)
    return (m.group(1).strip() if m else s)


def output_path(seg: Path, stem: str) -> Path:
    return seg.with_name(f"{stem}.요약.md")


def backup_path(seg: Path, stem: str) -> Path:
    return seg.with_name(f"{stem}.요약.prev.md")


def save(seg: Path, stem: str, body: str, fp: str, cmd: str) -> Path:
    out = output_path(seg, stem)
    if out.is_file():
        shutil.copy2(out, backup_path(seg, stem))
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    head = f"<!-- rec2md summarize: segments={fp} cmd={json.dumps(cmd, ensure_ascii=False)} at={stamp} -->\n"
    part = out.with_name(out.name + ".part")
    part.write_text(head + body.rstrip() + "\n", encoding="utf-8")
    os.replace(part, out)
    return out


def split_mark(text: str) -> tuple[dict, str]:
    """요약 파일 첫 줄의 rec2md 주석을 떼어 ({"segments": 지문, ...}, 본문)."""
    m = MARK.match(text)
    if not m:
        return {}, text
    info = dict(kv.split("=", 1) for kv in re.findall(r"(\w+=(?:\"(?:[^\"\\]|\\.)*\"|\S+))", m.group(1)))
    return info, text[m.end():]


class Lock:
    """같은 녹취에 요약이 두 번 동시에 돌지 않게 하는 OS 파일 잠금(맥·리눅스 flock, Windows msvcrt).
    다른 review 창(다른 프로세스)까지 막고, 쥔 프로세스가 죽으면 OS가 풀어 준다(낡은 잠금을 걷어낼 일이 없다).
    잠금 파일은 녹취 폴더를 어지럽히지 않게 임시 폴더에 둔다(이름은 segments.json 절대경로의 해시)."""

    def __init__(self, seg: Path, stem: str):
        key = hashlib.sha1(str(seg.resolve()).encode("utf-8")).hexdigest()[:16]
        self.path = Path(tempfile.gettempdir()) / f"rec2md-summarize-{key}.lock"
        self._fd: int | None = None

    def acquire(self) -> bool:
        if self._fd is not None:
            return False
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())   # 사람이 볼 참고용. 판정은 OS 잠금으로만 한다
        self._fd = fd
        return True

    def release(self) -> None:
        # 파일은 지우지 않는다: 지우면 다른 프로세스가 옛 파일에 잠금을 잡은 채 새 파일이 생기는 틈이 생긴다
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class Busy(RuntimeError):
    pass


class Job:
    """review 화면의 "요약 만들기" 한 건. 별도 스레드에서 돌고, 화면은 status()를 폴링한다."""

    def __init__(self, seg: Path, stem: str, cmd: str, timeout: float = TIMEOUT):
        import threading

        self.seg, self.stem, self.cmd, self.timeout = seg, stem, cmd, timeout
        self.lock = Lock(seg, stem)
        self._mu = threading.Lock()
        self._thread: threading.Thread | None = None
        self.state, self.error, self.started, self.finished = "idle", "", 0.0, 0.0

    def unavailable(self) -> str | None:
        return check_cmd(self.cmd)

    def start(self, t: Transcript, title: str) -> None:
        """t는 지금 화면 상태(편집·화자 이름 반영). 호출한 쪽이 복사본을 넘긴다."""
        import threading
        import time

        with self._mu:
            if self.state == "running":
                raise Busy("이미 요약을 만드는 중")
            if why := self.unavailable():
                raise SummarizeError(why)
            if not self.lock.acquire():
                raise Busy("다른 화면에서 같은 녹취의 요약을 만드는 중")
            self.state, self.error, self.started, self.finished = "running", "", time.time(), 0.0
        prompt, fp = build_prompt(t, title), fingerprint(t)
        self._thread = threading.Thread(target=self._work, args=(prompt, fp), daemon=True)
        self._thread.start()

    def _work(self, prompt: str, fp: str) -> None:
        import time

        try:
            body = run(self.cmd, prompt, self.timeout)
            save(self.seg, self.stem, body, fp, self.cmd)
            state, err = "done", ""
        except Exception as e:  # noqa: BLE001 — 무엇이 터져도 화면에 보이고 잠금은 풀려야 한다
            state, err = "error", str(e) or type(e).__name__
        finally:
            self.lock.release()
        with self._mu:
            self.state, self.error, self.finished = state, err, time.time()

    def wait(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def status(self) -> dict:
        import time

        with self._mu:
            end = self.finished or (time.time() if self.state == "running" else self.started)
            return {"state": self.state, "error": self.error, "cmd": self.cmd,
                    "elapsed": round(end - self.started, 1) if self.started else 0.0,
                    "disabled": self.unavailable()}
