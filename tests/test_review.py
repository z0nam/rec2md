"""rec2md review: 편집 로그 적용·저장, 오디오 Range, 판정 답 저장. 합성 데이터만 쓴다."""

from __future__ import annotations

import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from rec2md import cmd_review, speakers
from rec2md.review import Queue, Session, apply_edits, answers_path, targets
from rec2md.segments import Segment, Transcript

FIX = Path(__file__).parent / "fixtures"
LABELS = ["화자1", "화자2", "화자1", "화자2", "화자1", "화자3"]


def make(names: dict | None = None) -> Transcript:
    segs = [Segment(i * 10.0, i * 10.0 + 9, f"문장{i}", lb) for i, lb in enumerate(LABELS)]
    return Transcript("sample_dialogue.wav", 60.0, "test", "ko", segs, {"speaker_names": names or {}})


def labels(t: Transcript) -> list[str]:
    return [s.speaker for s in t.segments]


def edit(scope, seg, frm, to):
    return {"op": "set_speaker", "scope": scope, "seg": seg, "from": frm, "to": to}


# ---- apply_edits ----

def test_scope_one_after_all():
    t = make()
    assert labels(apply_edits(t, [edit("one", 2, "화자1", "화자3")])) == ["화자1", "화자2", "화자3", "화자2", "화자1", "화자3"]
    assert labels(apply_edits(t, [edit("after", 2, "화자1", "화자3")])) == ["화자1", "화자2", "화자3", "화자2", "화자3", "화자3"]
    assert labels(apply_edits(t, [edit("all", 2, "화자1", "화자3")])) == ["화자3", "화자2", "화자3", "화자2", "화자3", "화자3"]
    assert labels(t) == LABELS  # 원본은 그대로


def test_targets_match_preview():
    t = make()
    assert targets(t, "one", 3, "화자2") == [3]
    assert targets(t, "after", 2, "화자1") == [2, 4]
    assert targets(t, "all", 2, "화자1") == [0, 2, 4]


def test_new_name_all_names_the_label():
    """전체 + 새 이름 = 그 라벨에 사람이 정한 이름을 붙임 (자동 매칭 항목은 덮어씀)."""
    t = make({"화자2": {"name": "김철수", "score": 0.5, "tentative": True}})
    out = apply_edits(t, [edit("all", 1, "화자2", {"name": "박민수"})])
    assert labels(out) == LABELS
    assert out.meta["speaker_names"]["화자2"] == {"name": "박민수", "source": "user"}


def test_new_name_one_and_after_make_new_label():
    out = apply_edits(make(), [edit("after", 2, "화자1", {"name": "이영희"})])
    assert labels(out) == ["화자1", "화자2", "화자4", "화자2", "화자4", "화자3"]
    assert out.meta["speaker_names"] == {"화자4": speakers.user_entry("이영희")}


def test_existing_user_name_reuses_label():
    """사람이 이미 붙인 이름을 다시 입력하면 새 라벨을 만들지 않고 그 라벨로 옮긴다."""
    out = apply_edits(make(), [edit("all", 0, "화자1", {"name": "홍길동"}),
                               edit("one", 3, "화자2", {"name": "홍길동"})])
    assert labels(out) == ["화자1", "화자2", "화자1", "화자1", "화자1", "화자3"]
    assert out.meta["speaker_names"] == {"화자1": speakers.user_entry("홍길동")}


def test_unused_label_names_dropped():
    t = make({"화자3": {"name": "김철수", "score": 0.9}})
    out = apply_edits(t, [edit("all", 5, "화자3", "화자1")])
    assert "화자3" not in out.meta["speaker_names"]


def test_set_text_and_bad_ops():
    out = apply_edits(make(), [{"op": "set_text", "seg": 1, "from": "문장1", "to": "고친 문장"}])
    assert out.segments[1].text == "고친 문장"
    with pytest.raises(ValueError):
        apply_edits(make(), [edit("everything", 0, "화자1", "화자2")])
    with pytest.raises(ValueError):
        apply_edits(make(), [edit("one", 99, "화자1", "화자2")])
    with pytest.raises(ValueError):
        apply_edits(make(), [edit("one", 0, "화자1", {"name": "  "})])


def test_display_name():
    names = {"화자1": {"name": "홍길동", "score": 0.5, "tentative": True},
             "화자2": {"name": "김철수", "source": "user", "tentative": True}}
    assert speakers.display_name("화자1", names) == "홍길동?"
    assert speakers.display_name("화자2", names) == "김철수"      # 사람이 정한 이름엔 ? 없음
    assert speakers.display_name("화자3", names) == "화자3"


# ---- Session: 저장, 되돌리기, 왕복 ----

@pytest.fixture
def seg_file(tmp_path) -> Path:
    p = tmp_path / "meet.segments.json"
    make().to_json(p)
    return p


def test_session_save_undo_roundtrip(seg_file):
    s = Session(seg_file)
    s.add(edit("all", 0, "화자1", {"name": "홍길동"}))
    s.add(edit("one", 1, "화자2", "화자1"))
    d = seg_file.parent
    orig = d / "meet.segments.orig.json"
    assert orig.is_file() and labels(Transcript.from_json(orig)) == LABELS   # 원본 백업은 처음 한 번
    assert labels(Transcript.from_json(seg_file))[:2] == ["화자1", "화자1"]
    assert "홍길동(화자1)" in (d / "meet.raw.md").read_text(encoding="utf-8")
    log = json.loads((d / "meet.edits.json").read_text(encoding="utf-8"))
    assert [e["scope"] for e in log] == ["all", "one"] and all(e["at"] for e in log)

    s.undo()
    assert labels(Transcript.from_json(seg_file))[:2] == ["화자1", "화자2"]
    assert len(json.loads((d / "meet.edits.json").read_text(encoding="utf-8"))) == 1

    # 다시 열면 같은 상태, 같은 로그를 원본에 적용하면 같은 결과 (왕복)
    s2 = Session(seg_file)
    again = apply_edits(Transcript.from_json(orig), s2.edits)
    assert labels(again) == labels(s2.current) == labels(Transcript.from_json(seg_file))
    assert again.meta["speaker_names"] == Transcript.from_json(seg_file).meta["speaker_names"]


def test_session_rejects_bad_edit_without_logging(seg_file):
    s = Session(seg_file)
    with pytest.raises(ValueError):
        s.add(edit("one", 99, "화자1", "화자2"))
    assert s.edits == [] and not (seg_file.parent / "meet.edits.json").exists()


# ---- 판정 큐 ----

CASES = {"version": 1, "audio": "sample_dialogue.wav", "cases": [
    {"id": "A-1", "kind": "speaker", "start": 0.5, "end": 1.5, "question": "화자는?",
     "options": [{"key": "1", "label": "홍길동"}, {"key": "2", "label": "김철수"}]},
    {"id": "B-1", "kind": "term", "start": 1.0, "end": 2.0, "question": "표기는?",
     "options": [{"key": "1", "label": "교통량"}, {"key": "2", "label": "교통양"}], "allow_text": True}]}


@pytest.fixture
def cases_file(tmp_path) -> Path:
    p = tmp_path / "meet.cases.json"
    p.write_text(json.dumps(CASES, ensure_ascii=False), encoding="utf-8")
    return p


def test_queue_answers(cases_file):
    q = Queue(cases_file)
    q.answer("B-1", None, "교통 량", False)
    q.answer("A-1", "2", None, False)
    q.answer("A-1", "1", None, True)   # 다시 답하면 덮어씀, 모르겠음이면 key·text 비움
    data = json.loads(answers_path(cases_file).read_text(encoding="utf-8"))
    assert [a["id"] for a in data] == ["A-1", "B-1"]                       # 사례 순서
    assert data[0] | {"at": None} == {"id": "A-1", "key": None, "text": None, "unsure": True, "at": None}
    assert data[1]["text"] == "교통 량" and data[1]["key"] is None
    assert Queue(cases_file).answers == data                               # 이어서 하기
    with pytest.raises(ValueError):
        q.answer("A-1", "9", None, False)
    with pytest.raises(ValueError):
        q.answer("A-1", None, "직접", False)                               # allow_text 없는 사례
    with pytest.raises(ValueError):
        q.answer("Z-9", "1", None, False)


# ---- 서버 ----

@pytest.fixture
def server(tmp_path, seg_file):
    audio = tmp_path / "sample_dialogue.wav"
    shutil.copy(FIX / "sample_dialogue.wav", audio)
    app = cmd_review.App(Session(seg_file), None, cmd_review.find_audio(tmp_path, "sample_dialogue.wav"))
    httpd = cmd_review.serve(app, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", audio.stat().st_size
    httpd.shutdown()
    httpd.server_close()


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_audio_range(server):
    base, size = server
    st, h, body = get(base + "/audio", {"Range": "bytes=0-99"})
    assert st == 206 and h["Content-Range"] == f"bytes 0-99/{size}" and len(body) == 100
    st, h, body = get(base + "/audio", {"Range": "bytes=-10"})
    assert st == 206 and h["Content-Range"] == f"bytes {size - 10}-{size - 1}/{size}" and len(body) == 10
    st, h, body = get(base + "/audio", {"Range": f"bytes={size - 5}-"})
    assert st == 206 and len(body) == 5
    st, h, _ = get(base + "/audio", {"Range": f"bytes={size}-"})
    assert st == 416 and h["Content-Range"] == f"bytes */{size}"
    st, h, body = get(base + "/audio")
    assert st == 200 and len(body) == size and h["Accept-Ranges"] == "bytes"


def test_parse_range():
    assert cmd_review.parse_range("bytes=10-", 100) == (10, 99)
    assert cmd_review.parse_range("bytes=10-500", 100) == (10, 99)
    assert cmd_review.parse_range("items=1-2", 100) is None
    with pytest.raises(ValueError):
        cmd_review.parse_range("bytes=50-10", 100)


def test_api_edit_undo(server, seg_file):
    base, _ = server
    st, page = get(base + "/")[0], get(base + "/")[2]
    assert st == 200 and b"<audio" in page and b"http" not in page.split(b"<script>")[1]   # 외부 리소스 없음
    st, state = post(base + "/api/edit", edit("all", 0, "화자1", {"name": "홍길동"}))
    assert st == 200 and state["edits"] == 1 and state["segments"][0]["name"] == "홍길동"
    st, err = post(base + "/api/edit", edit("one", 99, "화자1", "화자2"))
    assert st == 400 and "범위" in err["error"]
    st, state = post(base + "/api/undo", {})
    assert state["edits"] == 0 and state["segments"][0]["name"] == "화자1"
    st, _ = post(base + "/api/answer", {"id": "x"})
    assert st == 404   # 검토 모드에선 판정 API 없음


def test_cli_requires_one_input(tmp_path):
    with pytest.raises(SystemExit, match="하나만"):
        cmd_review.main([])


def test_external_change_becomes_new_base(seg_file):
    """검토 뒤 voices name 등이 segments.json을 바꿨으면, 다시 열 때 그 내용을 버리지 않는다."""
    s = Session(seg_file)
    s.add(edit("one", 1, "화자2", "화자1"))
    t = Transcript.from_json(seg_file)                       # 다른 명령이 이름을 붙임
    t.meta["speaker_names"] = speakers.set_user_names(t.meta.get("speaker_names") or {}, {"화자3": "김철수"})
    t.to_json(seg_file)

    s2 = Session(seg_file)
    assert "새 원본" in s2.notice and s2.edits == []
    assert s2.current.meta["speaker_names"]["화자3"]["name"] == "김철수"
    d = seg_file.parent
    assert len(list(d.glob("meet.*.edits.json"))) == 1 and len(list(d.glob("meet.*.segments.orig.json"))) == 1
    s2.add(edit("one", 0, "화자1", "화자2"))
    out = Transcript.from_json(seg_file)
    assert out.meta["speaker_names"]["화자3"]["name"] == "김철수"     # 바깥 변경이 살아 있음
    assert labels(out)[:2] == ["화자2", "화자1"]                      # 지난 검토 결과 + 새 편집


def test_refuses_to_overwrite_change_made_while_open(seg_file):
    s = Session(seg_file)
    s.add(edit("one", 1, "화자2", "화자1"))
    t = Transcript.from_json(seg_file)
    t.segments[0].text = "다른 명령이 고침"
    t.to_json(seg_file)
    with pytest.raises(ValueError, match="다른 곳에서 바뀜"):
        s.add(edit("one", 0, "화자1", "화자2"))
    with pytest.raises(ValueError, match="다른 곳에서 바뀜"):
        s.undo()
    assert Transcript.from_json(seg_file).segments[0].text == "다른 명령이 고침" and len(s.edits) == 1


def test_reopen_without_external_change_keeps_log(seg_file):
    Session(seg_file).add(edit("one", 1, "화자2", "화자1"))
    s = Session(seg_file)
    assert s.notice == "" and len(s.edits) == 1
