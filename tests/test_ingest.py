"""rec2md ingest: 임시 폴더와 합성 바이트로만 시험한다(녹음 내용을 디코드하지 않으므로 오디오일 필요가 없다)."""

import json
import os

import pytest

from rec2md import cmd_ingest
from rec2md import ingest as ig


@pytest.fixture(autouse=True)
def small_quick(monkeypatch):
    monkeypatch.setattr(ig, "QUICK", 16)     # 빠른 지문이 앞뒤 일부만 읽는 경우를 작은 파일로 재현
    monkeypatch.setattr(ig, "CHUNK", 64)
    monkeypatch.setattr(ig, "SPACE_MARGIN", 0)


def _rec(d, name, data):
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)
    return d / name


def _data(seed, n=200):
    return bytes((seed * 31 + i * 7) % 256 for i in range(n))


@pytest.fixture
def dirs(tmp_path):
    rec, arc = tmp_path / "REC_DIR", tmp_path / "archive"
    rec.mkdir()
    return rec, arc


def _manifest(arc):
    return json.loads((arc / ig.MANIFEST).read_text(encoding="utf-8"))["files"]


def _mark_done(arc, name, outputs=None):
    out = arc / "_transcripts"
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / ".rec2md-state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    state[name] = {"status": "done", **({"outputs": outputs} if outputs else {})}
    state_path.write_text(json.dumps(state))
    stem = outputs or os.path.splitext(name)[0]
    (out / f"{stem}.segments.json").write_text("{}")


def test_copies_new_files_with_manifest_and_ignores_metadata(dirs):
    rec, arc = dirs
    _rec(rec, "REC00001.MP3", _data(1))
    _rec(rec, "._REC00001.MP3", b"apple double")
    _rec(rec, "notes.txt", b"x")
    (rec / "sub").mkdir()
    _rec(rec / "sub", "REC00009.MP3", _data(9))
    cmd_ingest.main([str(rec), str(arc)])
    assert sorted(p.name for p in arc.iterdir()) == [ig.MANIFEST, "REC00001.MP3"]
    assert (arc / "REC00001.MP3").read_bytes() == _data(1)
    (key, r), = _manifest(arc).items()
    assert key == f"{r['size']}:{r['sha1']}" and r["name"] == r["source_name"] == "REC00001.MP3"
    assert (rec / "REC00001.MP3").exists()   # 옵션 없이는 녹음기를 건드리지 않는다


def test_rerun_skips_known_files(dirs, capsys):
    rec, arc = dirs
    _rec(rec, "REC00001.MP3", _data(1))
    cmd_ingest.main([str(rec), str(arc)])
    mtime = (arc / "REC00001.MP3").stat().st_mtime_ns
    cmd_ingest.main([str(rec), str(arc)])
    assert ig.KNOWN in capsys.readouterr().out
    assert (arc / "REC00001.MP3").stat().st_mtime_ns == mtime
    assert len(_manifest(arc)) == 1


def test_rewound_number_gets_suffix_and_never_overwrites(dirs):
    rec, arc = dirs
    _rec(rec, "REC00001.MP3", _data(1))
    cmd_ingest.main([str(rec), str(arc)])
    (rec / "REC00001.MP3").unlink()
    _rec(rec, "REC00001.MP3", _data(2))        # 녹음기를 비운 뒤 같은 번호로 새 녹음
    cmd_ingest.main([str(rec), str(arc)])
    assert (arc / "REC00001.MP3").read_bytes() == _data(1)
    assert (arc / "REC00001-2.MP3").read_bytes() == _data(2)
    assert sorted(r["name"] for r in _manifest(arc).values()) == ["REC00001-2.MP3", "REC00001.MP3"]


def test_same_content_under_new_name_is_known(dirs, capsys):
    rec, arc = dirs
    _rec(rec, "REC00001.MP3", _data(1))
    cmd_ingest.main([str(rec), str(arc)])
    os.rename(rec / "REC00001.MP3", rec / "REC00005.MP3")
    cmd_ingest.main([str(rec), str(arc)])
    assert "예전 녹음기 이름 REC00001.MP3" in capsys.readouterr().out
    assert not (arc / "REC00005.MP3").exists()


def test_name_remembered_by_manifest_is_not_reused(dirs):
    rec, arc = dirs
    _rec(rec, "REC00001.MP3", _data(1))
    cmd_ingest.main([str(rec), str(arc)])
    os.rename(arc / "REC00001.MP3", arc.parent / "moved-away.MP3")   # 사람이 보관본을 다른 데로 옮김
    (rec / "REC00001.MP3").unlink()
    _rec(rec, "REC00001.MP3", _data(2))
    cmd_ingest.main([str(rec), str(arc)])
    assert (arc / "REC00001-2.MP3").read_bytes() == _data(2)
    assert not (arc / "REC00001.MP3").exists()


def test_adopts_manual_copy_without_manifest(dirs):
    rec, arc = dirs
    _rec(rec, "REC00001.MP3", _data(1))
    _rec(arc, "REC00001.MP3", _data(1))       # 예전에 손으로 복사해 둔 것
    cmd_ingest.main([str(rec), str(arc)])
    assert sorted(p.name for p in arc.iterdir()) == [ig.MANIFEST, "REC00001.MP3"]
    (r,) = _manifest(arc).values()
    assert r["adopted"] and r["name"] == "REC00001.MP3"


def test_different_manual_copy_is_kept_and_new_one_suffixed(dirs):
    rec, arc = dirs
    _rec(rec, "REC00001.MP3", _data(1))
    _rec(arc, "REC00001.MP3", _data(3))
    cmd_ingest.main([str(rec), str(arc)])
    assert (arc / "REC00001.MP3").read_bytes() == _data(3)
    assert (arc / "REC00001-2.MP3").read_bytes() == _data(1)


def test_dry_run_writes_nothing(dirs, capsys):
    rec, arc = dirs
    _rec(rec, "REC00001.MP3", _data(1))
    cmd_ingest.main([str(rec), str(arc), "--dry-run", "--move-processed", str(rec.parent / "done"),
                     "--batch", "--", "--remote", "studio"])
    out = capsys.readouterr().out
    assert ig.NEW in out and "--remote studio" in out and "받은 뒤 전사가 끝나야 옮김" in out
    assert not arc.exists() and not (rec.parent / "done").exists()


def test_copy_verification_failure_leaves_no_file(dirs, monkeypatch):
    rec, arc = dirs
    _rec(rec, "REC00001.MP3", _data(1))
    monkeypatch.setattr(ig, "full_sha1", lambda p, progress=None: "0" * 40)
    with pytest.raises(SystemExit, match="쓰기 오류"):
        cmd_ingest.main([str(rec), str(arc)])
    assert list(arc.iterdir()) == []     # 최종 이름도 .part도 매니페스트 기록도 남지 않는다


def test_does_not_overwrite_name_that_appears_during_copy(dirs, monkeypatch):
    rec, arc = dirs
    _rec(rec, "REC00001.MP3", _data(1))
    real = ig.full_sha1

    def sneaky(p, progress=None):        # 복사 확인 중에 같은 이름이 생김
        if p.name.endswith(".part"):
            (arc / "REC00001.MP3").write_bytes(b"someone else")
        return real(p, progress)
    monkeypatch.setattr(ig, "full_sha1", sneaky)
    with pytest.raises(SystemExit, match="덮어쓰지 않음"):
        cmd_ingest.main([str(rec), str(arc)])
    assert (arc / "REC00001.MP3").read_bytes() == b"someone else"
    assert (arc / "REC00001.MP3.part").read_bytes() == _data(1)


def test_move_processed_only_transcribed_and_matching(dirs, tmp_path):
    rec, arc = dirs
    done = tmp_path / "done"
    for i in (1, 2, 3):
        _rec(rec, f"REC0000{i}.MP3", _data(i))
    cmd_ingest.main([str(rec), str(arc)])
    _mark_done(arc, "REC00001.MP3")
    _mark_done(arc, "REC00003.MP3")
    # 3번은 녹음기 파일 가운데가 바뀜(빠른 지문은 같아 이미 받은 것으로 보이지만 sha1이 다르다)
    data = bytearray(_data(3))
    data[100] ^= 0xFF
    (rec / "REC00003.MP3").write_bytes(bytes(data))
    with pytest.raises(SystemExit, match="sha1이 보관본과 다름"):
        cmd_ingest.main([str(rec), str(arc), "--move-processed", str(done)])
    assert sorted(p.name for p in done.iterdir()) == ["REC00001.MP3"]
    assert sorted(p.name for p in rec.iterdir()) == ["REC00002.MP3", "REC00003.MP3"]
    (r1,) = [r for r in _manifest(arc).values() if r["name"] == "REC00001.MP3"]
    assert r1["moved_to"] == [str(done / "REC00001.MP3")]


def test_move_needs_segments_and_respects_renamed_outputs(dirs, tmp_path):
    rec, arc = dirs
    done = tmp_path / "done"
    _rec(rec, "REC00001.MP3", _data(1))
    _rec(rec, "REC00002.MP3", _data(2))
    cmd_ingest.main([str(rec), str(arc)])
    _mark_done(arc, "REC00001.MP3")
    (arc / "_transcripts" / "REC00001.segments.json").unlink()      # done이지만 결과가 없다
    _mark_done(arc, "REC00002.MP3", outputs="20261104-meeting")      # index rename으로 출력 이름이 바뀜
    cmd_ingest.main([str(rec), str(arc), "--move-processed", str(done)])
    assert sorted(p.name for p in done.iterdir()) == ["REC00002.MP3"]


def test_move_refuses_when_archive_copy_changed(dirs, tmp_path):
    rec, arc = dirs
    done = tmp_path / "done"
    _rec(rec, "REC00001.MP3", _data(1))
    cmd_ingest.main([str(rec), str(arc)])
    _mark_done(arc, "REC00001.MP3")
    (arc / "REC00001.MP3").write_bytes(_data(4))       # 같은 크기, 다른 내용
    with pytest.raises(SystemExit, match="보관본"):
        cmd_ingest.main([str(rec), str(arc), "--move-processed", str(done)])
    assert (rec / "REC00001.MP3").exists()


def test_move_never_overwrites_destination(dirs, tmp_path):
    rec, arc = dirs
    done = tmp_path / "done"
    _rec(rec, "REC00001.MP3", _data(1))
    _rec(done, "REC00001.MP3", b"older recording with the same number")
    cmd_ingest.main([str(rec), str(arc)])
    _mark_done(arc, "REC00001.MP3")
    cmd_ingest.main([str(rec), str(arc), "--move-processed", str(done)])
    assert (rec / "REC00001.MP3").exists()
    assert (done / "REC00001.MP3").read_bytes() == b"older recording with the same number"


def test_move_dest_must_not_be_recorder_or_other_disk(dirs, monkeypatch):
    rec, arc = dirs
    with pytest.raises(SystemExit, match="녹음기 폴더와 같음"):
        cmd_ingest.main([str(rec), str(arc), "--move-processed", str(rec)])
    monkeypatch.setattr(ig, "same_device", lambda a, b: False)
    with pytest.raises(SystemExit, match="같은 디스크"):
        cmd_ingest.main([str(rec), str(arc), "--move-processed", str(rec.parent / "done")])


def test_batch_args_are_forwarded(dirs, monkeypatch):
    from rec2md import cmd_batch
    rec, arc = dirs
    _rec(rec, "REC00001.MP3", _data(1))
    seen = []
    monkeypatch.setattr(cmd_batch, "main", lambda argv: seen.append(argv))
    cmd_ingest.main([str(rec), str(arc), "--batch", "--", "--remote", "studio", "--language", "en"])
    assert seen == [[str(arc), "--out", str(arc / "_transcripts"), "--remote", "studio", "--language", "en"]]


def test_batch_failure_still_moves_done_files_and_reports(dirs, tmp_path, monkeypatch):
    from rec2md import cmd_batch
    rec, arc = dirs
    done = tmp_path / "done"
    _rec(rec, "REC00001.MP3", _data(1))

    def fake_batch(argv):
        _mark_done(arc, "REC00001.MP3")
        raise SystemExit("실패 1개")
    monkeypatch.setattr(cmd_batch, "main", fake_batch)
    with pytest.raises(SystemExit, match="batch: 실패 1개"):
        cmd_ingest.main([str(rec), str(arc), "--batch", "--move-processed", str(done)])
    assert (done / "REC00001.MP3").exists()


def test_passthrough_without_batch_is_an_error(dirs):
    rec, arc = dirs
    with pytest.raises(SystemExit, match="--batch"):
        cmd_ingest.main([str(rec), str(arc), "--", "--remote", "studio"])


def test_free_space_check(dirs, monkeypatch):
    rec, arc = dirs
    _rec(rec, "REC00001.MP3", _data(1))
    monkeypatch.setattr(ig.shutil, "disk_usage", lambda p: type("U", (), {"free": 10})())
    with pytest.raises(SystemExit, match="공간 부족"):
        cmd_ingest.main([str(rec), str(arc)])
    assert not (arc / "REC00001.MP3").exists()


def test_quick_fingerprint_reads_head_and_tail_only(tmp_path):
    a = _rec(tmp_path, "a", _data(1))
    data = bytearray(_data(1))
    data[100] ^= 0xFF
    b = _rec(tmp_path, "b", bytes(data))
    assert ig.quick_fp(a) == ig.quick_fp(b) and ig.full_sha1(a) != ig.full_sha1(b)
    data[-1] ^= 0xFF
    c = _rec(tmp_path, "c", bytes(data))
    assert ig.quick_fp(a) != ig.quick_fp(c)
