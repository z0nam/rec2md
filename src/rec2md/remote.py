"""`rec2md <파일> --remote <host>`: 전사를 ssh로 다른 맥(studio)에 맡긴다.

녹음기는 Windows(namun-ji)에 꽂히는데 local 엔진(mlx)은 맥에서만 돈다. 그래서 Windows에서 한 줄로
원본 전송 → 원격 전사 → 결과 회수 → 원격 정리를 한다. 양쪽에 rec2md가 깔려 있어야 한다.

원격 작업 폴더 `~/.cache/rec2md/remote/<job>/` (job = YYYYMMDD-HHMMSS-<6자>)
  in/<원래 파일명>, in/ctx/<i>/<이름>  올린 원본과 --context 자료. 처리가 끝나면 성공·실패 모두 바로 지운다
  out/                              원격 run_one의 출력(segments.json, raw.md, voices.npz, corrections.json)
  log.txt, status.json              진행 로그와 상태 {"state": waiting|running|done|failed, "exit", "error"}

- 원격 처리는 `nohup`으로 띄우므로 ssh가 끊겨도 계속된다. 로컬은 짧은 ssh로 상태를 확인하고, 끝내 연결이
  안 되면 job id를 알려 주고 끝낸다 → 나중에 `rec2md remote fetch <job>`.
- 원격 job끼리는 `remote/.lock`(flock)으로 한 번에 하나씩 돈다. 락 밖의 rec2md(직접 돌린 batch 등)와는
  겹칠 수 있어 시작할 때 경고만 한다.
- 디스크: 올리기 전에 여유 공간이 원본·자료 크기의 3배 이상인지 본다. 성공한 job은 회수 뒤 통째로 지우고,
  실패한 job은 로그·상태만 7일 남긴다(원본은 로컬에 있다). 7일 지난 실패·고아 job은 다음 실행 때 지우고,
  끝났지만 아직 받지 않은 결과와 돌고 있는 job은 남긴다.
- 원격 쪽 명령은 원격 rec2md의 숨은 서브커맨드(`rec2md remote _prepare|_run|_status|_clean`)다.
  비대화형 ssh의 PATH에는 ~/.local/bin이 없어 실행 파일을 경로로 부른다(기본 `.local/bin/rec2md`,
  홈 기준 상대경로. 바꾸려면 REC2MD_REMOTE_BIN).
- ssh·scp는 셸 없이 인자 리스트로 부르고 stdin을 막는다(Windows OpenSSH는 -n 없이 부르면 입력을 기다리며 멈춘다).
"""

from __future__ import annotations

import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = ".cache/rec2md/remote"     # 원격 홈 기준
DEFAULT_BIN = ".local/bin/rec2md"
KEEP_DAYS = 7
SPACE_FACTOR = 3                  # 여유 공간 ≥ 올릴 크기 × 이 배수
POLL_SEC = 15.0
POLL_RETRIES = 40                 # 연속 실패 허용 횟수(백오프 최대 60초 → 대략 30분)
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=4"]
JOBS_DIR = Path.home() / ".cache" / "rec2md" / "remote-jobs"   # 로컬: job → 출력 폴더 기록


def log(msg: str) -> None:
    print(f"[rec2md] {msg}", file=sys.stderr, flush=True)


class RemoteError(RuntimeError):
    """원격 처리 실패. 일반 예외라 batch가 파일별 실패로 잡는다. 단일 실행은 cli.main이 SystemExit로 바꾼다."""


def new_job_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def job_dir(job: str) -> str:
    if not job or "/" in job or "\\" in job or job.startswith("."):
        raise RemoteError(f"잘못된 job id: {job!r}")
    return f"{ROOT}/{job}"


def remote_bin() -> str:
    return os.environ.get("REC2MD_REMOTE_BIN") or DEFAULT_BIN


# ---------------------------------------------------------------- 로컬 쪽

Run = Callable[..., subprocess.CompletedProcess]


@dataclass
class Host:
    """ssh/scp 호출. run은 테스트에서 가짜로 바꾼다."""
    name: str
    run: Run = field(default=subprocess.run)

    def ssh(self, *words: str, raw: str = "", prefix: str = "", check: bool = True) -> subprocess.CompletedProcess:
        """원격 rec2md에 인자를 넘긴다. words는 POSIX 인용, prefix·raw는 앞뒤에 그대로 붙인다."""
        cmd = " ".join([*([prefix] if prefix else []), remote_bin(), *map(shlex.quote, words)])
        cmd += f" {raw}" if raw else ""
        r = self.run(["ssh", "-n", *SSH_OPTS, self.name, cmd], stdin=subprocess.DEVNULL,
                     capture_output=True, text=True, encoding="utf-8", errors="replace")
        if check and r.returncode != 0:
            raise RemoteError(f"원격 명령 실패({self.name}, exit {r.returncode}): {(r.stderr or r.stdout).strip()}")
        return r

    def ssh_json(self, *words: str) -> dict:
        r = self.ssh(*words)
        try:
            return json.loads(r.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            raise RemoteError(f"원격 응답을 읽을 수 없음: {r.stdout.strip()[:200]} {r.stderr.strip()[:200]}") from None

    def scp(self, srcs: list[str], dst: str, recursive: bool = False) -> None:
        r = self.run(["scp", *SSH_OPTS, *(["-r"] if recursive else []), *srcs, dst], stdin=subprocess.DEVNULL,
                     capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise RemoteError(f"파일 전송 실패(exit {r.returncode}): {r.stderr.strip()}")

    def put(self, local: Path, remote_dir: str) -> None:
        self.scp([str(local)], f"{self.name}:{remote_dir}/")

    def get_dir(self, remote_dir: str, local: Path) -> None:
        self.scp([f"{self.name}:{remote_dir}"], str(local), recursive=True)


def forward_args(args) -> list[str]:
    """전사 옵션을 원격 rec2md 인자로 되돌린다. 입력·--out·--remote·--context는 따로 다룬다."""
    from .cli import build_parser
    skip = {"inputs", "out", "remote", "context", "help"}
    words: list[str] = []
    for a in build_parser(inputs=False)._actions:
        if a.dest in skip or not a.option_strings or not hasattr(args, a.dest):
            continue
        v = getattr(args, a.dest)
        if v == a.default:
            continue
        flag = a.option_strings[-1]
        if a.nargs == 0:   # store_true / store_false: 기본값과 다르면 플래그만
            words.append(flag)
        elif isinstance(v, list):
            for x in v:
                words += [flag, str(x)]
        else:
            words += [flag, str(v)]
    return words


def _record(job: str, host: str, out_dir: Path, stem: str) -> Path:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    p = JOBS_DIR / f"{job}.json"
    p.write_text(json.dumps({"host": host, "out": str(out_dir.resolve()), "stem": stem}, ensure_ascii=False),
                 encoding="utf-8")
    return p


def load_record(job: str) -> dict:
    p = JOBS_DIR / f"{job}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def run_remote(src: Path, args, host: Host | None = None, poll: float = POLL_SEC,
               sleep: Callable[[float], None] = time.sleep) -> Path:
    """run_one의 원격판. 결과를 로컬 출력 폴더에 받고 raw.md 경로를 돌려준다."""
    host = host or Host(args.remote)
    out_dir = Path(args.out) if args.out else src.parent
    ctx = [Path(c) for c in (getattr(args, "context", None) or [])]
    for c in ctx:
        if not c.is_file():
            raise RemoteError(f"--context 파일 없음: {c}")
    need = src.stat().st_size + sum(c.stat().st_size for c in ctx)
    job = new_job_id()
    jd = job_dir(job)

    prep = host.ssh_json("remote", "_prepare", job, "--need", str(need), "--ctx", str(len(ctx)))
    if prep.get("others"):
        log(f"주의: {host.name}에서 다른 rec2md가 돌고 있음(GPU를 나눠 씀): " + "; ".join(prep["others"])[:300])
    if not prep.get("ok"):
        raise RemoteError(f"{host.name} 디스크 여유 부족: 필요 {_gb(need * SPACE_FACTOR)} (원본 크기 × "
                          f"{SPACE_FACTOR}), 여유 {_gb(prep.get('free', 0))}")
    _record(job, host.name, out_dir, src.stem)
    log(f"{host.name}로 전송: {src.name} ({_gb(need)}) → job {job}")
    try:
        host.put(src, f"{jd}/in")
        for i, c in enumerate(ctx):
            host.put(c, f"{jd}/in/ctx/{i}")
    except RemoteError:
        host.ssh("remote", "_clean", job, check=False)
        raise
    words = ["remote", "_run", job, "--", src.name, *forward_args(args)]
    for i, c in enumerate(ctx):
        words += ["--context", f"ctx/{i}/{c.name}"]
    log_path = shlex.quote(f"{jd}/log.txt")
    # 출력을 모두 돌려 놓고 백그라운드로 띄우면 ssh는 바로 돌아온다. nohup은 연결이 끊길 때의 SIGHUP 대비
    host.ssh(*words, prefix="nohup", raw=f"> {log_path} 2>&1 < /dev/null &")
    return wait_and_fetch(host, job, out_dir, src.stem, poll=poll, sleep=sleep)


def wait_and_fetch(host: Host, job: str, out_dir: Path, stem: str, poll: float = POLL_SEC,
                   sleep: Callable[[float], None] = time.sleep, offset: int = 0) -> Path:
    fails, st = 0, {}
    while True:
        try:
            st = host.ssh_json("remote", "_status", job, "--from", str(offset))
            fails = 0
        except RemoteError as e:
            fails += 1
            if fails > POLL_RETRIES:
                raise RemoteError(f"{host.name} 연결이 계속 끊김({e}). 원격 처리는 계속됨 → 나중에 "
                                  f"`rec2md remote fetch {job}`") from None
            sleep(min(60.0, poll * fails))
            continue
        for line in st.get("log", "").splitlines():
            print(f"  {host.name}| {line}", file=sys.stderr, flush=True)
        offset = st.get("offset", offset)
        if st.get("state") in ("done", "failed"):
            break
        sleep(poll)
    if st["state"] == "failed":
        raise RemoteError(f"원격 전사 실패(job {job}, exit {st.get('exit')}): {st.get('error') or '로그 참고'}. "
                          f"로그는 {host.name}:~/{job_dir(job)}/log.txt에 {KEEP_DAYS}일 남음")
    raw = fetch(host, job, out_dir)
    host.ssh("remote", "_clean", job, check=False)
    (JOBS_DIR / f"{job}.json").unlink(missing_ok=True)
    return raw if raw else out_dir / f"{stem}.raw.md"


def fetch(host: Host, job: str, out_dir: Path) -> Path | None:
    """원격 out/을 로컬 임시 폴더로 받은 뒤 파일별로 바꿔 끼운다(반쯤 받은 파일이 남지 않게)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / f".rec2md-remote-{job}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    try:
        host.get_dir(f"{job_dir(job)}/out", tmp)
        got = tmp / "out"
        files = sorted(p for p in got.iterdir() if p.is_file()) if got.is_dir() else []
        if not files:
            raise RemoteError(f"원격 결과가 비어 있음(job {job})")
        raw = None
        for p in files:
            dst = out_dir / p.name
            os.replace(p, dst)
            if p.name.endswith(".raw.md"):
                raw = dst
            log(f"← {dst}")
        return raw
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _gb(n: float) -> str:
    return f"{n / 1024 ** 3:.1f}GiB"


# ---------------------------------------------------------------- 원격 쪽 (studio에서 실행)

def _home_job(job: str) -> Path:
    return Path.home() / job_dir(job)


def _write_status(jd: Path, **st) -> None:
    tmp = jd / "status.json.tmp"
    tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, jd / "status.json")


def other_rec2md() -> list[str]:
    """이 job 밖에서 돌고 있는 rec2md 프로세스(명령줄)."""
    try:
        out = subprocess.run(["ps", "-Ao", "pid=,command="], capture_output=True, text=True, check=False).stdout
    except OSError:
        return []
    me = {os.getpid(), os.getppid()}
    found = []
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        words = cmd.split()
        if not pid.isdigit() or int(pid) in me or not words or " remote _" in cmd:
            continue
        # 실행 파일이 python이나 rec2md인 것만(셸 명령줄에 문자열로 들어 있는 것은 빼고)
        exe = Path(words[0]).name
        if (exe.startswith("python") or exe == "rec2md") and any(w.endswith("bin/rec2md") for w in words[:2]):
            found.append(cmd.strip()[:160])
    return found


EXTRA_PATH = ("/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local" / "bin"))


def extend_path(env: dict) -> None:
    """비대화형 ssh의 PATH(/usr/bin:/bin:…)에는 brew의 ffmpeg가 없다. 흔한 위치를 뒤에 붙인다."""
    have = env.get("PATH", "").split(os.pathsep)
    env["PATH"] = os.pathsep.join([*filter(None, have), *(d for d in EXTRA_PATH if d not in have)])


def _keep(d: Path) -> bool:
    """아직 받지 않은 성공 결과와 돌고 있는 job은 오래돼도 남긴다(원본은 이미 지워져 크기가 작다)."""
    try:
        st = json.loads((d / "status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if st.get("state") == "done":
        return True
    return st.get("state") in ("waiting", "running") and "pid" in st and _alive(st["pid"])


def sweep(root: Path, keep_days: float = KEEP_DAYS, now: float | None = None) -> int:
    """keep_days보다 오래된 실패·고아 job 폴더를 지운다."""
    if not root.is_dir():
        return 0
    cutoff = (now or time.time()) - keep_days * 86400
    n = 0
    for d in root.iterdir():
        if d.is_dir() and not d.name.startswith(".") and d.stat().st_mtime < cutoff and not _keep(d):
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n


def prepare(job: str, need: int, n_ctx: int = 0) -> dict:
    root = Path.home() / ROOT
    root.mkdir(parents=True, exist_ok=True)
    swept = sweep(root)
    jd = _home_job(job)
    free = shutil.disk_usage(root).free
    others = other_rec2md()
    if free < need * SPACE_FACTOR:
        return {"ok": False, "free": free, "need": need, "others": others, "swept": swept}
    (jd / "in").mkdir(parents=True)
    for i in range(n_ctx):
        (jd / "in" / "ctx" / str(i)).mkdir(parents=True)
    (jd / "out").mkdir()
    _write_status(jd, state="waiting")
    return {"ok": True, "free": free, "need": need, "others": others, "swept": swept}


def run_job(job: str, argv: list[str]) -> int:
    """argv[0]은 in/ 안의 원본 이름, 나머지는 전사 옵션(--context는 in/ 기준 상대경로)."""
    import fcntl

    from . import cli
    jd = _home_job(job)
    inp = jd / "in"
    extend_path(os.environ)
    name, rest = argv[0], argv[1:]
    fixed: list[str] = []
    it = iter(rest)
    for w in it:   # --context 경로를 in/ 기준으로
        fixed.append(w)
        if w == "--context":
            fixed.append(str(inp / next(it)))
    code, err = 1, ""
    _write_status(jd, state="waiting", pid=os.getpid())
    with open(Path.home() / ROOT / ".lock", "w") as lock:   # 프로세스가 죽으면 OS가 락을 푼다
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("[rec2md] 다른 원격 job이 끝나기를 기다림", file=sys.stderr, flush=True)
            fcntl.flock(lock, fcntl.LOCK_EX)
        _write_status(jd, state="running", pid=os.getpid())
        try:
            cli.main([str(inp / name), "--out", str(jd / "out"), *fixed])
            code = 0
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            err = "" if isinstance(e.code, int) else str(e.code)
        except Exception as e:  # noqa: BLE001 — 원인을 status에 남기고 로그에도 찍는다
            import traceback
            traceback.print_exc()
            err = f"{type(e).__name__}: {e}"
        finally:
            shutil.rmtree(inp, ignore_errors=True)   # 원본은 로컬에 있다. 디스크가 빠듯해 바로 지운다
    _write_status(jd, state="done" if code == 0 else "failed", exit=code, error=err)
    return code


def status(job: str, offset: int = 0) -> dict:
    jd = _home_job(job)
    if not jd.is_dir():
        return {"state": "missing", "log": "", "offset": offset}
    try:
        st = json.loads((jd / "status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        st = {"state": "waiting"}
    text, new = "", offset
    lp = jd / "log.txt"
    if lp.is_file():
        with lp.open("rb") as f:
            f.seek(offset)
            data = f.read()
        cut = data.rfind(b"\n") + 1   # 줄 단위로만 넘긴다(쓰는 중인 줄·잘린 UTF-8 방지)
        text, new = data[:cut].decode("utf-8", "replace"), offset + cut
    if st.get("state") in ("waiting", "running") and "pid" in st and not _alive(st["pid"]):
        st = {"state": "failed", "exit": None, "error": "원격 프로세스가 사라짐(재부팅·강제 종료?)"}
    return {**st, "log": text, "offset": new}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def clean(job: str) -> None:
    shutil.rmtree(_home_job(job), ignore_errors=True)


def list_jobs() -> list[dict]:
    root = Path.home() / ROOT
    rows = []
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        if d.is_dir() and not d.name.startswith("."):
            rows.append({"job": d.name, **{k: v for k, v in status(d.name).items() if k not in ("log", "offset")}})
    return rows
