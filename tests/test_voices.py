"""목소리 DB·매칭 단위 테스트 (pyannote 없이 가짜 벡터로)."""

from __future__ import annotations

import stat

import numpy as np
import pytest

from rec2md import cmd_voices, voices
from rec2md.render import render_raw, speaker_label
from rec2md.segments import Segment, Transcript

DIM = 256
rng = np.random.default_rng(0)
BASE = {name: rng.normal(size=DIM) for name in ["홍길동", "김철수", "이영희"]}


def noisy(name: str, scale: float = 0.3) -> np.ndarray:
    return BASE[name] + rng.normal(scale=scale, size=DIM) * np.linalg.norm(BASE[name]) / np.sqrt(DIM)


def write_rec(tmp_path, stem: str, speakers: dict[str, tuple[np.ndarray, float]]):
    """segments.json + voices.npz 한 쌍. speakers: {화자N: (임베딩, 초)}"""
    seg = tmp_path / f"{stem}.segments.json"
    segs = [Segment(i * 10.0, i * 10.0 + 5, f"발화 {i}", lb) for i, lb in enumerate(speakers)]
    Transcript(source=f"{stem}.wav", duration=60.0, engine="test", language="ko", segments=segs).to_json(seg)
    turns = [(0.0, sec, f"SPK_{lb}") for lb, (_, sec) in speakers.items()]
    voices.save_sidecar(voices.sidecar_path(seg), {f"SPK_{lb}": v for lb, (v, _) in speakers.items()},
                        turns, {f"SPK_{lb}": lb for lb in speakers})
    return seg


def test_sidecar_roundtrip_keeps_final_labels(tmp_path):
    p = tmp_path / "a.voices.npz"
    embs = {"SPEAKER_00": np.ones(4), "SPEAKER_01": np.arange(4.0), "SPEAKER_02": np.ones(4)}
    turns = [(0, 10, "SPEAKER_01"), (10, 15, "SPEAKER_00"), (20, 25, "SPEAKER_01")]
    # SPEAKER_02는 전사 단어가 안 붙어 화자N이 없음 → 빠져야 한다
    voices.save_sidecar(p, embs, turns, {"SPEAKER_01": "화자1", "SPEAKER_00": "화자2"})
    side = voices.load_sidecar(p)
    assert set(side) == {"화자1", "화자2"}
    assert side["화자1"][1] == pytest.approx(15.0)
    np.testing.assert_allclose(side["화자1"][0], np.arange(4.0))


def test_db_permissions_and_crud(tmp_path):
    path = tmp_path / "v" / "voices.db"
    with voices.VoiceDB.open(path) as db:
        assert db.add("홍길동", noisy("홍길동"), "a.wav", "화자1", 40)
        assert not db.add("홍길동", noisy("홍길동"), "a.wav", "화자1", 40)  # 같은 녹취·화자는 한 번만
        db.add("홍길동", noisy("홍길동"), "b.wav", "화자2", 50)
        db.add("김철수", noisy("김철수"), "a.wav", "화자2", 35)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with voices.VoiceDB.open(path) as db:
        assert db.people() == [("김철수", 1, 35.0), ("홍길동", 2, 90.0)]
        db.rename("김철수", "김영수")
        assert db.forget("홍길동") == 2
        assert [r[0] for r in db.people()] == ["김영수"]
        with pytest.raises(SystemExit):
            db.forget("없는사람")


def test_enroll_rejects_short_cluster():
    db = voices.VoiceDB(":memory:")
    side = {"화자1": (noisy("홍길동"), 45.0), "화자2": (noisy("김철수"), 12.0)}
    with pytest.raises(SystemExit, match="30초 미만"):
        voices.enroll(db, side, {"화자1": "홍길동", "화자2": "김철수"}, "a.wav")
    assert db.people() == []  # 하나라도 거절이면 아무것도 등록하지 않는다
    voices.enroll(db, side, {"화자1": "홍길동"}, "a.wav")
    assert [r[0] for r in db.people()] == ["홍길동"]


def test_match_one_to_one_threshold_and_split():
    db = voices.VoiceDB(":memory:")
    for i, name in enumerate(BASE):
        for k in range(2):
            db.add(name, noisy(name), f"rec{k}.wav", f"화자{i}", 60)
    people = db.vectors()
    stranger = rng.normal(size=DIM)
    clusters = {
        "화자1": (noisy("김철수"), 100.0),
        "화자2": (noisy("홍길동"), 80.0),
        "화자3": (stranger, 50.0),
        "화자4": (noisy("김철수"), 20.0),  # pyannote가 한 사람을 쪼갠 경우
    }
    out = voices.match(people, clusters, threshold=0.6)
    assert out["화자1"]["name"] == "김철수" and "tentative" not in out["화자1"]
    assert out["화자2"]["name"] == "홍길동"
    assert "화자3" not in out  # 모르는 사람은 이름을 안 붙인다
    assert out["화자4"]["name"] == "김철수" and out["화자4"]["tentative"] and out["화자4"]["same_as"] == "화자1"
    assert voices.match(people, clusters, threshold=0.999) == {}


def test_tentative_band():
    people = {"홍길동": voices._unit(np.array([1.0, 0.0]))[None]}
    score = 0.62
    q = np.array([score, np.sqrt(1 - score**2)])
    out = voices.match(people, {"화자1": (q, 60)}, threshold=0.6, margin=0.05)
    assert out["화자1"]["tentative"]


def test_render_names():
    names = {"화자1": {"name": "홍길동", "score": 0.8}, "화자2": {"name": "김철수", "score": 0.61, "tentative": True}}
    assert speaker_label("화자1", names) == "홍길동(화자1)"
    assert speaker_label("화자2", names) == "김철수?(화자2)"
    assert speaker_label("화자3", names) == "화자3"
    t = Transcript(source="a.wav", duration=10, engine="x", language="ko",
                   segments=[Segment(0, 1, "안녕", "화자1"), Segment(1, 2, "네", "화자2")],
                   meta={"speaker_names": names})
    md = render_raw(t, "a")
    assert "**[00:00:00] 홍길동(화자1)** 안녕" in md and "김철수?(화자2)" in md


def test_cli_enroll_then_match(tmp_path, capsys):
    db = str(tmp_path / "voices.db")
    a = write_rec(tmp_path, "a", {"화자1": (noisy("홍길동"), 60), "화자2": (noisy("김철수"), 45)})
    cmd_voices.main(["--db", db, "enroll", str(a), "--map", "화자1=홍길동, 화자2=김철수"])
    b = write_rec(tmp_path, "b", {"화자1": (noisy("김철수"), 60), "화자2": (noisy("홍길동"), 45),
                                  "화자3": (noisy("이영희"), 30)})
    (tmp_path / "b.raw.md").write_text("old", encoding="utf-8")
    cmd_voices.main(["--db", db, "match", str(b)])
    names = Transcript.from_json(b).meta["speaker_names"]
    assert {k: v["name"] for k, v in names.items()} == {"화자1": "김철수", "화자2": "홍길동"}
    assert "김철수" in (tmp_path / "b.raw.md").read_text(encoding="utf-8")
    cmd_voices.main(["--db", db, "list"])
    assert "홍길동\t임베딩 1개" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="같은 이름"):
        cmd_voices.main(["--db", db, "enroll", str(b), "--map", "화자1=홍길동,화자2=홍길동"])


def test_cli_enroll_from_user_names(tmp_path, capsys):
    db = str(tmp_path / "voices.db")
    a = write_rec(tmp_path, "a", {"화자1": (noisy("홍길동"), 60), "화자2": (noisy("김철수"), 45),
                                  "화자3": (noisy("홍길동"), 35), "화자4": (noisy("이영희"), 10)})
    t = Transcript.from_json(a)
    t.meta["speaker_names"] = {
        "화자1": {"name": "홍길동", "source": "user"},
        "화자2": {"name": "김철수", "score": 0.8},  # 자동 매칭 결과는 등록 대상이 아니다
        "화자3": {"name": "홍길동", "source": "user"},  # 쪼개진 화자 → 긴 쪽(화자1)만
        "화자4": {"name": "이영희", "source": "user"},  # 30초 미만 → 건너뜀
    }
    t.to_json(a)
    cmd_voices.main(["--db", db, "enroll", str(a), "--from-names"])
    out = capsys.readouterr().out
    assert "화자4 → 이영희 건너뜀" in out
    with voices.VoiceDB.open(db) as vdb:
        assert vdb.people() == [("홍길동", 1, 60.0)]


def test_match_keeps_user_names(tmp_path):
    db = str(tmp_path / "voices.db")
    a = write_rec(tmp_path, "a", {"화자1": (noisy("홍길동"), 60), "화자2": (noisy("김철수"), 45)})
    cmd_voices.main(["--db", db, "enroll", str(a), "--map", "화자1=홍길동,화자2=김철수"])
    b = write_rec(tmp_path, "b", {"화자1": (noisy("홍길동"), 60), "화자2": (noisy("김철수"), 45)})
    t = Transcript.from_json(b)
    t.meta["speaker_names"] = {"화자1": {"name": "박사장", "source": "user"}}  # 사용자가 고친 이름이 우선
    t.to_json(b)
    cmd_voices.main(["--db", db, "match", str(b)])
    names = Transcript.from_json(b).meta["speaker_names"]
    assert names["화자1"] == {"name": "박사장", "source": "user"}
    assert names["화자2"]["name"] == "김철수"


def test_robust_enroll_drops_other_speaker(tmp_path):
    # 클러스터에 다른 사람 구간이 섞였으면 그 구간은 빼고 등록한다
    center = noisy("홍길동", 0.1)
    segs = [(i * 10.0, i * 10.0 + 5, noisy("홍길동")) for i in range(6)] + [(100.0, 108.0, noisy("김철수"))]
    vec, purity, dropped = voices.robust_vector(center, segs)
    assert dropped == [(100.0, 108.0)]
    assert purity == pytest.approx(30 / 38)
    assert float(vec @ voices._unit(BASE["홍길동"])) > float(voices._unit(center) @ voices._unit(BASE["홍길동"])) - 0.05
    # 구간이 적으면 중심 그대로
    assert voices.robust_vector(center, segs[:2])[1:] == (1.0, [])

    db = voices.VoiceDB(":memory:")
    msgs = voices.enroll(db, {"화자1": (center, 60.0)}, {"화자1": "홍길동"}, "a.wav", segments={"화자1": segs})
    assert any("경고" in m and "00:01:40" in m for m in msgs)


def test_sidecar_segments_roundtrip(tmp_path):
    p = tmp_path / "a.voices.npz"
    embs = {"S0": np.ones(4), "S1": np.arange(4.0)}
    segs = [(0.0, 2.0, "S0", np.ones(4)), (3.0, 5.0, "S1", np.arange(4.0)), (6.0, 8.0, "S2", np.ones(4))]
    voices.save_sidecar(p, embs, [(0, 2, "S0"), (3, 5, "S1")], {"S0": "화자1", "S1": "화자2"}, segs)
    got = voices.load_segments(p)
    assert set(got) == {"화자1", "화자2"} and got["화자2"][0][:2] == (3.0, 5.0)


def test_below_threshold_edges_do_not_displace_valid_match(monkeypatch):
    # [[0.80, 0.74], [0.74, 0.00]]: 전체 합만 보면 0.74 두 개를 고르지만 0.80 짝이 살아야 한다
    m = np.array([[0.80, 0.74], [0.74, 0.00]], dtype=np.float32)
    monkeypatch.setattr(voices, "score_matrix", lambda p, c: (["화자1", "화자2"], ["홍길동", "김철수"], m))
    clusters = {"화자1": (np.ones(2), 60), "화자2": (np.ones(2), 60)}
    got = voices.match({"홍길동": np.ones((1, 2)), "김철수": np.ones((1, 2))}, clusters, threshold=0.75)
    assert got == {"화자1": {"name": "홍길동", "score": 0.8}}


def test_sidecar_empty_mapping_and_permissions(tmp_path):
    p = tmp_path / "a.voices.npz"
    p.write_bytes(b"old")
    p.chmod(0o644)
    voices.save_sidecar(p, {"S0": np.ones(4)}, [(0, 2, "S0")], {}, [(0.0, 2.0, "S0", np.ones(4))])
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert voices.load_sidecar(p) == {} and voices.load_segments(p) == {}


def test_cli_name_then_enroll_from_names(tmp_path, capsys):
    db = str(tmp_path / "voices.db")
    a = write_rec(tmp_path, "a", {"화자1": (noisy("홍길동"), 60), "화자2": (noisy("김철수"), 45)})
    (tmp_path / "a.raw.md").write_text("old", encoding="utf-8")
    cmd_voices.main(["--db", db, "name", str(a), "--map", "화자1=홍길동,화자2=김철수"])
    assert "홍길동(화자1)" in (tmp_path / "a.raw.md").read_text(encoding="utf-8")
    cmd_voices.main(["--db", db, "name", str(a), "--unset", "화자2"])
    assert Transcript.from_json(a).meta["speaker_names"] == {"화자1": {"name": "홍길동", "source": "user"}}
    cmd_voices.main(["--db", db, "enroll", str(a), "--from-names"])
    with voices.VoiceDB.open(db) as vdb:
        assert [r[0] for r in vdb.people()] == ["홍길동"]
    with pytest.raises(SystemExit, match="없는 화자"):
        cmd_voices.main(["--db", db, "name", str(a), "--map", "화자9=누구"])
