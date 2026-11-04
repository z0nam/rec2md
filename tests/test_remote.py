"""--remote: ssh·scp를 가짜로 바꿔 원격 쪽(cmd_remote 내부 명령)을 임시 HOME에서 그대로 돌린다."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from rec2md import cli, cmd_remote, remote


class FakeNet:
    """ssh/scp 대역. 원격 홈은 HOME 환경변수(테스트마다 임시 폴더)다."""

    def __init__(self, home: Path):
        self.home = home
        self.calls: list[list[str]] = []
        self.fail_ssh = 0          # 앞으로 몇 번의 ssh를 연결 실패로 만들지

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        assert kw.get("stdin") is subprocess.DEVNULL
        if argv[0] == "ssh":
            assert argv[1] == "-n"
            return self._ssh(argv[-1])
        return self._scp(argv)

    def _ssh(self, cmd: str):
        if self.fail_ssh:
            self.fail_ssh -= 1
            return subprocess.CompletedProcess([], 255, "", "Connection timed out")
        words = shlex.split(cmd)
        if words[0] == "nohup":
            words = words[1:]
        assert words[0] == remote.DEFAULT_BIN
        words = words[1:]
        log = None
        if ">" in words:   # "> log 2>&1 < /dev/null &"
            i = words.index(">")
            log, words = self.home / words[i + 1], words[:i]
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main(words)
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
                if not isinstance(e.code, int) and e.code is not None:
                    print(e.code, file=err)
        if log is not None:
            log.write_text(out.getvalue() + err.getvalue(), encoding="utf-8")
            return subprocess.CompletedProcess([], 0, "", "")
        return subprocess.CompletedProcess([], code, out.getvalue(), err.getvalue())

    def _path(self, s: str) -> Path:
        if s.startswith("studio:"):
            return self.home / s.removeprefix("studio:")
        return Path(s)

    def _scp(self, argv):
        args = [a for a in argv[1:] if a not in remote.SSH_OPTS]
        rec = "-r" in args
        args = [a for a in args if a != "-r"]
        *srcs, dst = args
        d = self._path(dst)
        for s in srcs:
            sp = self._path(s)
            if not sp.exists():
                return subprocess.CompletedProcess([], 1, "", f"scp: {sp}: No such file")
            if rec:
                shutil.copytree(sp, d / sp.name)
            else:
                if not d.is_dir():
                    return subprocess.CompletedProcess([], 1, "", f"scp: {d}: not a directory")
                shutil.copy(sp, d / sp.name)
        return subprocess.CompletedProcess([], 0, "", "")


@pytest.fixture
def net(tmp_path, monkeypatch):
    home = tmp_path / "studio-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("REC2MD_REMOTE_BIN", raising=False)
    monkeypatch.setattr(remote, "JOBS_DIR", tmp_path / "local-jobs")
    monkeypatch.setattr(remote, "other_rec2md", list)
    return FakeNet(home)


def fake_transcribe(seen: dict):
    """원격 run_one 대신: 받은 인자를 기록하고 결과 파일을 만든다."""
    def run_one(src, args):
        seen.update(src=src, args=args, src_exists=src.is_file(),
                    ctx=[(p, p.is_file()) for p in (args.context or [])])
        out = Path(args.out)
        (out / f"{src.stem}.segments.json").write_text('{"segments": []}', encoding="utf-8")
        (out / f"{src.stem}.raw.md").write_text("# raw\n", encoding="utf-8")
        (out / f"{src.stem}.voices.npz").write_bytes(b"npz")
        print("[rec2md] 전사 끝", file=__import__("sys").stderr)
        return out / f"{src.stem}.raw.md"
    return run_one


def args_for(argv):
    return cli.build_parser().parse_args(argv)


def run(src: Path, argv: list[str], net: FakeNet) -> Path:
    a = args_for([str(src), "--remote", "studio", *argv])
    return remote.run_remote(src, a, host=remote.Host("studio", run=net), poll=0, sleep=lambda s: None)


def test_forward_args_round_trip():
    a = args_for(["x.mp3", "--remote", "studio", "--out", "o", "--speakers", "3", "--no-trim",
                  "--series", "주간회의", "--no-prompt", "--trim-min-gap", "2", "--context", "c.md"])
    w = remote.forward_args(a)
    assert w == ["--speakers", "3", "--no-trim", "--trim-min-gap", "2.0", "--series", "주간회의", "--no-prompt"]
    back = args_for(["x.mp3", *w])
    for k in ("speakers", "no_trim", "trim_min_gap", "series", "prompt", "engine", "language"):
        assert getattr(back, k) == getattr(a, k)
    assert remote.forward_args(args_for(["x.mp3"])) == []


def test_round_trip_korean_name_and_context(tmp_path, net, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(cli, "run_one", fake_transcribe(seen))
    src = tmp_path / "win" / "가상 회의 1.mp3"
    src.parent.mkdir()
    src.write_bytes(b"\0" * 100)
    ctx = tmp_path / "win" / "자료.md"
    ctx.write_text("용어", encoding="utf-8")
    out = tmp_path / "결과"
    raw = run(src, ["--out", str(out), "--speakers", "2", "--context", str(ctx)], net)

    assert raw == out / "가상 회의 1.raw.md"
    assert sorted(p.name for p in out.iterdir()) == ["가상 회의 1.raw.md", "가상 회의 1.segments.json",
                                                      "가상 회의 1.voices.npz"]
    assert seen["src_exists"] and seen["src"].name == "가상 회의 1.mp3"
    assert seen["args"].speakers == 2 and seen["args"].remote is None
    assert seen["ctx"][0][1] and seen["ctx"][0][0].name == "자료.md"
    assert not any((net.home / remote.ROOT).glob("2*")), "성공한 job은 원격에서 지운다"
    assert not list(remote.JOBS_DIR.glob("*.json")), "로컬 기록도 지운다"
    launch = next(c[-1] for c in net.calls if c[0] == "ssh" and " _run " in c[-1])
    assert launch.startswith("nohup .local/bin/rec2md remote _run ") and launch.endswith("< /dev/null &")
    assert "'가상 회의 1.mp3'" in launch


def test_failure_keeps_log_and_drops_original(tmp_path, net, monkeypatch):
    def boom(src, args):
        print("[rec2md] 화자분리 중", file=__import__("sys").stderr)
        raise SystemExit("모델 로드 실패")
    monkeypatch.setattr(cli, "run_one", boom)
    src = tmp_path / "a.mp3"
    src.write_bytes(b"x" * 10)
    with pytest.raises(remote.RemoteError, match="원격 전사 실패.*모델 로드 실패"):
        run(src, [], net)
    (jd,) = (net.home / remote.ROOT).glob("2*")
    assert not (jd / "in").exists(), "실패해도 원본은 바로 지운다"
    assert json.loads((jd / "status.json").read_text())["state"] == "failed"
    assert "화자분리 중" in (jd / "log.txt").read_text(encoding="utf-8")
    assert len(list(remote.JOBS_DIR.glob("*.json"))) == 1, "fetch로 다시 볼 수 있게 기록은 남긴다"


def test_not_enough_space(tmp_path, net, monkeypatch):
    monkeypatch.setattr(remote.shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(100, 80, 20))
    src = tmp_path / "a.mp3"
    src.write_bytes(b"x" * 10)   # 10 × 3 = 30 > 20
    with pytest.raises(remote.RemoteError, match="디스크 여유 부족"):
        run(src, [], net)
    assert not any((net.home / remote.ROOT).glob("2*"))
    assert not any(c[0] == "scp" for c in net.calls)


def test_sweep_old_jobs(tmp_path):
    root = tmp_path / "r"
    dead = subprocess.Popen(["true"])
    dead.wait()
    jobs = {   # 이름: (status, 지워져야 하나)
        "old-failed": ({"state": "failed"}, True),
        "old-nostatus": (None, True),
        "old-dead": ({"state": "running", "pid": dead.pid}, True),
        "old-done": ({"state": "done", "exit": 0}, False),         # 아직 안 받은 결과
        "old-running": ({"state": "running", "pid": os.getpid()}, False),
    }
    t = time.time() - 8 * 86400
    for name, (st, _) in jobs.items():
        d = root / name
        d.mkdir(parents=True)
        if st is not None:
            (d / "status.json").write_text(json.dumps(st))
        os.utime(d, (t, t))
    new = root / "new-failed"
    new.mkdir()
    (new / "status.json").write_text('{"state": "failed"}')
    (root / ".lock").write_text("")
    assert remote.sweep(root) == 3
    for name, (_, gone) in jobs.items():
        assert (root / name).exists() != gone, name
    assert new.exists() and (root / ".lock").exists()


def test_status_reads_whole_lines_only(net):
    jd = net.home / remote.ROOT / "j1"
    jd.mkdir(parents=True)
    (jd / "status.json").write_text(json.dumps({"state": "running", "pid": os.getpid()}))
    (jd / "log.txt").write_bytes("한 줄\n쓰는 중".encode())
    st = remote.status("j1")
    assert st["log"] == "한 줄\n" and st["offset"] == len("한 줄\n".encode())
    assert remote.status("j1", st["offset"])["log"] == ""
    assert remote.status("없음")["state"] == "missing"


def test_dead_process_is_failed(net):
    jd = net.home / remote.ROOT / "j2"
    jd.mkdir(parents=True)
    p = subprocess.Popen(["true"])
    p.wait()
    (jd / "status.json").write_text(json.dumps({"state": "running", "pid": p.pid}))
    assert remote.status("j2")["state"] == "failed"


def test_poll_survives_drops_then_gives_up(tmp_path, net, monkeypatch):
    monkeypatch.setattr(cli, "run_one", fake_transcribe({}))
    src = tmp_path / "a.mp3"
    src.write_bytes(b"x")
    real_ssh = net._ssh
    state = {"launched": False}

    def flaky(cmd):   # 띄운 뒤 첫 두 번의 상태 확인이 끊긴다
        r = real_ssh(cmd)
        if " _run " in cmd:
            state["launched"], net.fail_ssh = True, 2
        return r
    monkeypatch.setattr(net, "_ssh", flaky)
    assert run(src, [], net).name == "a.raw.md"

    monkeypatch.setattr(remote, "POLL_RETRIES", 3)
    net.fail_ssh = 0
    state["launched"] = False

    def drops(cmd):
        r = real_ssh(cmd)
        if " _run " in cmd:
            net.fail_ssh = 99
        return r
    monkeypatch.setattr(net, "_ssh", drops)
    with pytest.raises(remote.RemoteError, match=r"rec2md remote fetch \d{8}-\d{6}-[0-9a-f]{6}"):
        run(src, [], net)


def test_fetch_after_drop(tmp_path, net, monkeypatch, capsys):
    monkeypatch.setattr(cli, "run_one", fake_transcribe({}))
    monkeypatch.setattr(remote, "POLL_RETRIES", 0)
    src = tmp_path / "a.mp3"
    src.write_bytes(b"x")
    real_ssh = net._ssh

    def drops(cmd):
        r = real_ssh(cmd)
        if " _run " in cmd:
            net.fail_ssh = 1
        return r
    monkeypatch.setattr(net, "_ssh", drops)
    out = tmp_path / "o"
    with pytest.raises(remote.RemoteError) as e:
        run(src, ["--out", str(out)], net)
    job = str(e.value).split("rec2md remote fetch ")[1].rstrip("`")
    rec = remote.load_record(job)
    assert rec["host"] == "studio" and Path(rec["out"]) == out.resolve()
    raw = remote.wait_and_fetch(remote.Host("studio", run=net), job, Path(rec["out"]), rec["stem"], poll=0,
                                sleep=lambda s: None)
    assert raw == out.resolve() / "a.raw.md" and raw.is_file()


def test_bad_job_id():
    for bad in ("", "../x", "a/b", ".lock"):
        with pytest.raises(remote.RemoteError):
            remote.job_dir(bad)


def test_cli_routes_remote(tmp_path, monkeypatch):
    called = {}
    monkeypatch.setattr(remote, "run_remote", lambda src, args: called.setdefault("src", src))
    src = tmp_path / "a.mp3"
    src.write_bytes(b"x")
    cli.main([str(src), "--remote", "studio"])
    assert called["src"] == src
    assert "remote" in cli.SUBCOMMANDS and callable(cmd_remote.main)


def test_extend_path_adds_brew_once():
    env = {"PATH": "/usr/bin:/bin"}
    remote.extend_path(env)
    remote.extend_path(env)
    parts = env["PATH"].split(os.pathsep)
    assert parts[:2] == ["/usr/bin", "/bin"] and parts.count("/opt/homebrew/bin") == 1


def test_other_rec2md_ignores_shell_strings(monkeypatch):
    ps = ("  11 /u/.local/share/uv/tools/rec2md/bin/python /u/.local/bin/rec2md batch in --out o\n"
          "  12 /bin/zsh -c cd x && /u/.local/bin/rec2md a.mp3\n"
          "  13 /u/.venv/bin/python /u/.venv/bin/rec2md remote _prepare j --need 1\n")
    monkeypatch.setattr(remote.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, ps, ""))
    found = remote.other_rec2md()
    assert len(found) == 1 and "batch" in found[0]


def test_batch_continues_after_remote_failure(tmp_path, monkeypatch):
    from rec2md import cmd_batch
    d = tmp_path / "in"
    d.mkdir()
    for n in ("a.wav", "b.wav"):
        (d / n).write_bytes(b"x")
    monkeypatch.setattr(cmd_batch.audio, "probe_duration", lambda p: 60.0)
    monkeypatch.setattr(cmd_batch, "speech_sec", lambda p, g=None: 60.0)
    tried = []

    def fake(src, args):
        tried.append(src.name)
        raise remote.RemoteError("원격 전사 실패")
    monkeypatch.setattr(remote, "run_remote", fake)
    try:
        cmd_batch.main([str(d), "--remote", "studio"])
    except SystemExit:
        pass
    assert tried == ["a.wav", "b.wav"], "한 파일이 원격에서 실패해도 다음 파일로 넘어간다"
    state = json.loads((d / "_transcripts" / ".rec2md-state.json").read_text(encoding="utf-8"))
    assert {k: v["status"] for k, v in state.items()} == {"a.wav": "failed", "b.wav": "failed"}


def test_cli_turns_remote_error_into_exit(tmp_path, monkeypatch):
    def fail(src, args):
        raise remote.RemoteError("원격 전사 실패(job x)")
    monkeypatch.setattr(remote, "run_remote", fail)
    src = tmp_path / "a.mp3"
    src.write_bytes(b"x")
    with pytest.raises(SystemExit, match=r"원격 전사 실패\(job x\)"):
        cli.main([str(src), "--remote", "studio"])


def test_run_captured_uses_files_not_pipes(monkeypatch):
    seen = {}
    real = subprocess.run

    def spy(argv, **kw):
        seen.update(kw)
        return real(argv, **kw)
    monkeypatch.setattr(remote.subprocess, "run", spy)
    r = remote.run_captured(["python3", "-c", "import sys; print('한글 ok'); print('err', file=sys.stderr)"])
    assert r.returncode == 0 and r.stdout == "한글 ok\n" and r.stderr == "err\n"
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["stdout"] not in (subprocess.PIPE, None) and seen["stderr"] not in (subprocess.PIPE, None)
    assert "capture_output" not in seen


def test_run_captured_timeout():
    r = remote.run_captured(["python3", "-c", "import time; time.sleep(5)"], timeout=0.5)
    assert r.returncode == 124 and "끝나지 않음" in r.stderr
