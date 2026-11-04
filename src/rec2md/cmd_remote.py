"""rec2md remote: `--remote`로 맡긴 원격 job 관리.

  rec2md remote list   [--host studio]          원격 job 목록과 상태
  rec2md remote status <job> [--host studio]     상태와 로그 끝부분
  rec2md remote fetch  <job> [--host studio] [--out <dir>]
                                                 끝난 job 결과 받기(연결이 끊겨 중단된 뒤 이어 받기). 덜 끝났으면 기다림
  rec2md remote clean  <job> [--host studio]     원격 job 폴더 지우기

--host 기본값은 이 job을 맡긴 기록(~/.cache/rec2md/remote-jobs/), 없으면 REC2MD_REMOTE, 그것도 없으면 studio.
`_prepare`·`_run`·`_status`·`_clean`·`_list`는 원격 쪽에서 로컬 rec2md가 부르는 내부 명령이다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import remote


def _out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=True))   # 원격 셸·Windows 콘솔 인코딩과 무관하게 읽히도록 ASCII로


def _internal(cmd: str, argv: list[str]) -> None:
    if cmd == "_run":
        if len(argv) < 3 or argv[1] != "--":
            raise SystemExit("사용법: rec2md remote _run <job> -- <파일명> [전사 옵션 …]")
        raise SystemExit(remote.run_job(argv[0], argv[2:]))
    p = argparse.ArgumentParser(prog=f"rec2md remote {cmd}")
    if cmd != "_list":
        p.add_argument("job")
    if cmd == "_prepare":
        p.add_argument("--need", type=int, required=True)
        p.add_argument("--ctx", type=int, default=0)
    if cmd == "_status":
        p.add_argument("--from", dest="offset", type=int, default=0)
    a = p.parse_args(argv)
    if cmd == "_prepare":
        _out(remote.prepare(a.job, a.need, a.ctx))
    elif cmd == "_status":
        _out(remote.status(a.job, a.offset))
    elif cmd == "_clean":
        remote.clean(a.job)
        _out({"ok": True})
    elif cmd == "_list":
        _out(remote.list_jobs())
    else:
        raise SystemExit(f"알 수 없는 내부 명령: {cmd}")


def _host(a) -> remote.Host:
    rec = remote.load_record(a.job) if getattr(a, "job", None) else {}
    return remote.Host(a.host or rec.get("host") or os.environ.get("REC2MD_REMOTE") or "studio")


def main(argv: list[str]) -> None:
    if argv and argv[0].startswith("_"):
        return _internal(argv[0], argv[1:])
    p = argparse.ArgumentParser(prog="rec2md remote", description="--remote로 맡긴 원격 전사 job 관리")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("list", "status", "fetch", "clean"):
        s = sub.add_parser(name)
        if name != "list":
            s.add_argument("job")
        s.add_argument("--host")
        if name == "fetch":
            s.add_argument("--out", help="받을 폴더 (기본: 맡길 때의 출력 폴더, 기록이 없으면 현재 폴더)")
    a = p.parse_args(argv)
    host = _host(a)
    if a.cmd == "list":
        rows = host.ssh_json("remote", "_list")
        if not rows:
            print("원격 job 없음")
        for r in rows:
            print(f"{r['job']}\t{r.get('state')}\t{r.get('error') or ''}")
    elif a.cmd == "status":
        st = host.ssh_json("remote", "_status", a.job)
        print(f"{a.job}: {st.get('state')}" + (f" (exit {st['exit']})" if st.get("exit") is not None else ""))
        if st.get("error"):
            print(st["error"])
        tail = st.get("log", "").splitlines()[-20:]
        if tail:
            print("--- 로그 끝부분")
            print("\n".join(tail))
    elif a.cmd == "fetch":
        rec = remote.load_record(a.job)
        out = Path(a.out or rec.get("out") or ".")
        st = host.ssh_json("remote", "_status", a.job)
        if st.get("state") == "missing":
            raise SystemExit(f"{host.name}에 job {a.job}이 없음(이미 받았거나 정리됨)")
        # 로그는 이미 본 것일 수 있어 끝까지 건너뛰고 새 줄만 보여 준다
        raw = remote.wait_and_fetch(host, a.job, out, rec.get("stem", ""), offset=st.get("offset", 0))
        print(raw)
    elif a.cmd == "clean":
        host.ssh("remote", "_clean", a.job)
        remote.JOBS_DIR.joinpath(f"{a.job}.json").unlink(missing_ok=True)
        print(f"{host.name}: job {a.job} 지움", file=sys.stderr)
