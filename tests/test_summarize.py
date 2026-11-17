"""요약 만들기(summarize.py, review 요약 탭). 실제 에이전트 CLI는 부르지 않는다:
stdin을 받아 고정 md를 내는 가짜 명령으로 계약만 확인한다. 이름은 가짜 값만 쓴다."""

from __future__ import annotations

import json
import os
import shlex
import sys
import threading
import time
from pathlib import Path

import pytest

from rec2md import cmd_review, summarize
from rec2md.review import Session, summary_file
from rec2md.segments import Segment, Transcript

FAKE = """\
import sys, pathlib, time
prompt = sys.stdin.read()
pathlib.Path(sys.argv[1]).write_text(prompt, encoding="utf-8")
mode = sys.argv[2] if len(sys.argv) > 2 else "ok"
if mode == "fail":
    sys.stderr.write("로그인 필요\\n"); sys.exit(3)
if mode == "slow":
    time.sleep(float(sys.argv[3]))
if mode == "empty":
    sys.exit(0)
print("```markdown\\n## 개요\\n- 가짜 요약 [00:10]\\n```")
"""


def make(names=None, bounds=None) -> Transcript:
    segs = [Segment(i * 10.0, i * 10.0 + 9, f"문장{i}", lb) for i, lb in enumerate(["화자1", "화자2", "화자1"])]
    meta = {"speaker_names": names or {}}
    if bounds:
        meta["meeting_bounds"] = bounds
    return Transcript("meet.wav", 30.0, "test", "ko", segs, meta)


@pytest.fixture
def seg_file(tmp_path) -> Path:
    p = tmp_path / "meet.segments.json"
    make({"화자1": {"name": "홍길동", "source": "user"}}).to_json(p)
    return p


@pytest.fixture
def fake(tmp_path):
    script = tmp_path / "fake_summ.py"
    script.write_text(FAKE, encoding="utf-8")
    seen = tmp_path / "seen_prompt.txt"

    def cmd(mode="ok", *extra):
        return " ".join(shlex.quote(x) for x in (sys.executable, str(script), str(seen), mode, *map(str, extra)))
    cmd.seen = seen
    return cmd


# ---- 명령 고르기 ----

def test_resolve_cmd_order(monkeypatch):
    monkeypatch.delenv(summarize.ENV_CMD, raising=False)
    assert summarize.resolve_cmd() == "claude -p"
    monkeypatch.setenv(summarize.ENV_CMD, "codex exec")
    assert summarize.resolve_cmd() == "codex exec"
    assert summarize.resolve_cmd("my-llm --md") == "my-llm --md"


def test_check_cmd_missing():
    assert summarize.check_cmd("없는명령-rec2md-test -p") is not None
    assert "비어" in summarize.check_cmd("  ")
    assert summarize.check_cmd(f"{shlex.quote(sys.executable)} -c pass") is None


# ---- 프롬프트 ----

def test_prompt_has_guide_names_and_bounds():
    t = make({"화자1": {"name": "홍길동", "source": "user"}},
             {"start": 10.0, "end": 25.0, "confidence": "medium", "evidence": ["앞 10초 잡담"]})
    p = summarize.build_prompt(t, "운영위")
    assert p.startswith(summarize.GUIDE)
    assert "홍길동 00:00" in p and "문장1" in p                 # 화면에서 붙인 이름이 들어간다
    assert "00:10〜00:25 (신뢰도 medium)" in p and "앞 10초 잡담" in p
    assert "추정: 없음" in summarize.build_prompt(make(), "x")


def test_guide_matches_skill_md():
    """SKILL.md 2단계 3·4번이 지침을 가리키고, 지침에 네 갈래 형식이 다 있다(SSOT 어긋남 방지)."""
    skill = (Path(__file__).parents[1] / ".claude/skills/rec2md/SKILL.md").read_text(encoding="utf-8")
    assert "review --print-guide" in skill
    for h in ("## 개요", "## 주요 논의", "## 결정사항", "## 할 일", "회의 구간"):
        assert h in summarize.GUIDE


# ---- 실행·저장 ----

def test_run_strips_fence_and_reports_failure(fake):
    assert summarize.run(fake(), "프롬프트") == "## 개요\n- 가짜 요약 [00:10]"
    assert fake.seen.read_text(encoding="utf-8") == "프롬프트"
    with pytest.raises(summarize.SummarizeError, match="종료 코드 3: 로그인 필요"):
        summarize.run(fake("fail"), "x")
    with pytest.raises(summarize.SummarizeError, match="비어"):
        summarize.run(fake("empty"), "x")
    with pytest.raises(summarize.SummarizeError, match="초 안에"):
        summarize.run(fake("slow", 5), "x", timeout=0.5)


def test_save_atomic_with_one_backup(seg_file):
    out = summarize.save(seg_file, "meet", "## 개요\n- 첫째", "aaa", "fake")
    assert out.name == "meet.요약.md"
    info, body = summarize.split_mark(out.read_text(encoding="utf-8"))
    assert info["segments"] == "aaa" and body == "## 개요\n- 첫째\n"
    summarize.save(seg_file, "meet", "## 개요\n- 둘째", "bbb", "fake")
    assert "첫째" in seg_file.with_name("meet.요약.prev.md").read_text(encoding="utf-8")
    assert "둘째" in out.read_text(encoding="utf-8")
    assert not list(seg_file.parent.glob("*.part"))


def test_lock_is_exclusive_and_outside_transcript_folder(seg_file):
    lk = summarize.Lock(seg_file, "meet")
    assert lk.path.parent != seg_file.parent       # 녹취 폴더를 어지럽히지 않는다
    assert lk.acquire()
    other = summarize.Lock(seg_file, "meet")       # 다른 창(별도 파일 디스크립터)
    assert not other.acquire()
    lk.release()
    assert other.acquire()
    other.release()


def test_lock_held_by_other_process(seg_file):
    """다른 프로세스가 쥐면 못 잡고, 그 프로세스가 끝나면(죽어도) OS가 풀어 준다."""
    import subprocess

    lk = summarize.Lock(seg_file, "meet")
    code = ("import sys, time; sys.path[:0] = sys.argv[3:]\n"
            "from pathlib import Path; from rec2md import summarize\n"
            "l = summarize.Lock(Path(sys.argv[1]), 'meet'); assert l.acquire()\n"
            "print('ok', flush=True); time.sleep(float(sys.argv[2]))")
    p = subprocess.Popen([sys.executable, "-c", code, str(seg_file), "30", *sys.path], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "ok"
        assert not lk.acquire()
    finally:
        p.kill()
        p.wait()
    assert lk.acquire()
    lk.release()


def test_relative_command_runs_from_temp_cwd(tmp_path, monkeypatch):
    """./wrapper 같은 상대경로 명령도 빈 임시 폴더에서 돌 때 찾는다."""
    w = tmp_path / "wrapper.sh"
    w.write_text("#!/bin/sh\ncat >/dev/null\necho '## 개요'\n", encoding="utf-8")
    w.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    assert summarize.check_cmd("./wrapper.sh") is None
    assert summarize.run("./wrapper.sh", "x") == "## 개요"


# ---- Session 패널 ----

def test_summary_prefers_generated_and_skips_prev(seg_file):
    seg_file.with_name("meet.녹취록.md").write_text("손으로", encoding="utf-8")
    seg_file.with_name("meet.요약.prev.md").write_text("예전", encoding="utf-8")
    assert summary_file(seg_file, "meet").name == "meet.녹취록.md"
    seg_file.with_name("meet.요약.md").write_text("만든 것", encoding="utf-8")
    assert summary_file(seg_file, "meet").name == "meet.요약.md"


def test_panel_stale_after_edit(seg_file):
    s = Session(seg_file)
    summarize.save(seg_file, "meet", "## 개요", summarize.fingerprint(s.current), "fake")
    p = s.panel()["summary"]
    assert p["generated"] and not p["stale"] and not p["text"].startswith("<!--")
    s.add({"op": "set_text", "seg": 1, "from": "문장1", "to": "고침"})
    assert s.panel()["summary"]["stale"]
    s.undo()
    assert not s.panel()["summary"]["stale"]       # 되돌리면 다시 맞는다


def test_handwritten_summary_stale_by_mtime(seg_file):
    s = Session(seg_file)
    hand = seg_file.with_name("meet.녹취록.md")
    hand.write_text("# 요약", encoding="utf-8")
    assert s.panel()["summary"]["stale"] is False
    os.utime(hand, (time.time() - 100, time.time() - 100))
    s.add({"op": "set_text", "seg": 1, "from": "문장1", "to": "고침"})
    assert s.panel()["summary"] == {"name": "meet.녹취록.md", "text": "# 요약", "generated": False, "stale": True}
    assert hand.read_text(encoding="utf-8") == "# 요약"


# ---- Job (비동기) ----

def test_job_runs_once_and_writes(seg_file, fake):
    s = Session(seg_file)
    job = summarize.Job(seg_file, "meet", fake("slow", 0.5))
    job.start(s.current, "meet")
    with pytest.raises(summarize.Busy):
        job.start(s.current, "meet")
    assert summarize.Job(seg_file, "meet", fake()).lock.acquire() is False   # 다른 창도 막힘
    job.wait(10)
    st = job.status()
    assert st["state"] == "done" and st["elapsed"] >= 0.5 and st["disabled"] is None
    assert "가짜 요약" in seg_file.with_name("meet.요약.md").read_text(encoding="utf-8")
    assert summarize.Lock(seg_file, "meet").acquire()   # 끝나면 풀린다


def test_job_error_releases_lock(seg_file, fake):
    job = summarize.Job(seg_file, "meet", fake("fail"))
    job.start(Session(seg_file).current, "meet")
    job.wait(10)
    assert job.status()["state"] == "error" and "로그인 필요" in job.status()["error"]
    assert job.lock._fd is None and not seg_file.with_name("meet.요약.md").exists()


def test_job_missing_command_disabled(seg_file):
    job = summarize.Job(seg_file, "meet", "없는명령-rec2md-test -p")
    assert "PATH에 없음" in job.status()["disabled"]
    with pytest.raises(summarize.SummarizeError):
        job.start(Session(seg_file).current, "meet")


# ---- 서버 ----

def _server(seg_file, cmd):
    app = cmd_review.App(Session(seg_file), None, None, summarize_cmd=cmd)
    httpd = cmd_review.serve(app, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return app, httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def test_api_summarize_flow(seg_file, fake):
    from test_review import get, post  # 같은 폴더의 도우미

    app, httpd, base = _server(seg_file, fake("slow", 0.3))
    try:
        st, r = post(base + "/api/edit", {"op": "set_speaker", "scope": "all", "seg": 1, "from": "화자2",
                                           "to": {"name": "김철수"}})
        assert st == 200
        st, r = post(base + "/api/summarize", {})
        assert st == 202 and r["state"] == "running"
        st, r = post(base + "/api/summarize", {})
        assert st == 409
        app.job.wait(10)
        _, _, body = get(base + "/api/summarize")
        assert json.loads(body)["state"] == "done"
        assert "김철수" in fake.seen.read_text(encoding="utf-8")   # 화면에서 고친 이름이 입력에 반영
        _, _, body = get(base + "/api/panel")
        p = json.loads(body)["summary"]
        assert p["name"] == "meet.요약.md" and p["generated"] and not p["stale"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_api_summarize_missing_command(seg_file):
    from test_review import get, post

    _, httpd, base = _server(seg_file, "없는명령-rec2md-test -p")
    try:
        _, _, body = get(base + "/api/summarize")
        assert "PATH에 없음" in json.loads(body)["disabled"]
        st, r = post(base + "/api/summarize", {})
        assert st == 400 and "PATH에 없음" in r["error"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_print_guide(capsys):
    cmd_review.main(["--print-guide"])
    assert capsys.readouterr().out == summarize.GUIDE
