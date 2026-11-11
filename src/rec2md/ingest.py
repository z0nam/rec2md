"""녹음기 폴더 → 보관 폴더 수집(`rec2md ingest`). 상주 감시 없이 사람이 부르는 명령의 부품이다.

녹음기 번호 규칙 때문에 지키는 것 (이슈 #40):
- 녹음기는 `RECnnnnn` 형식 파일의 마지막 번호 + 1을 새 녹음에 붙이는 것으로 보인다. 파일을 옮기거나 지우면 번호가
  되감겨 **같은 이름의 다른 녹음**이 생긴다. 그래서 녹음은 파일명이 아니라 지문(크기 + sha1)으로 구분하고,
  매니페스트의 키도 지문이다.
- 보관 폴더에 같은 이름이 있으면 덮어쓰지 않는다. 지문이 같으면 이미 받은 것, 다르면 `-2`, `-3` 접미사로 따로 둔다.

녹음기가 느려서(USB로 약 0.5MB/s, 1.5GB에 1시간 가까이) 읽는 횟수를 아낀다:
- 이미 받았는지는 **빠른 지문**(크기 + 앞뒤 64KB의 sha1)으로 판단한다. 파일마다 128KB만 읽어,
  녹음기에 수백 개가 남아 있어도 1분 남짓이면 훑는다. 크기와 앞뒤 몇 초가 바이트까지 같은 다른 녹음은 사실상 없다.
- 새 파일은 한 번만 읽는다. 읽으면서 sha1을 계산해 `.part`에 쓰고, 다 쓴 `.part`를 보관 디스크에서 다시 읽어
  sha1이 같을 때만 최종 이름으로 바꾼다.
- 녹음기에서 처리완료 폴더로 옮길 때는 전체 sha1이 맞아야 한다. 이번 실행에서 복사하며 계산한 값이 있으면 그것을 쓰고,
  없으면(예전 실행에서 받은 파일) 녹음기 파일을 다시 끝까지 읽는다.

맥에서 녹음기를 열면 생기는 `._` 메타 파일과 숨김 파일은 보지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

MEDIA_EXTS = {".mp3", ".m4a", ".wav", ".mp4", ".mov"}
MANIFEST = ".rec2md-ingest.json"
CHUNK = 1 << 20          # 1MB
QUICK = 64 << 10         # 빠른 지문에 쓰는 앞뒤 길이(128kbps MP3로 앞뒤 약 4초)
SPACE_MARGIN = 1 << 30   # 복사 후에도 보관 디스크에 남겨 둘 여유(1GB)
PROGRESS_SEC = 30.0      # 긴 복사의 진행 로그 간격

# 수집 판정
NEW, KNOWN, RENAMED, RESTORE = "새 파일", "이미 받음", "새 파일(이름 겹침)", "보관본 다시 받음"
COPY = (NEW, RENAMED, RESTORE)   # 복사할 판정


def log(msg: str) -> None:
    from .cli import log as _log
    _log(msg)


def media_files(d: Path) -> list[Path]:
    """녹음기 폴더 바로 아래의 녹음 파일. `._*`(맥 메타)와 숨김 파일, `.part`는 뺀다."""
    return sorted(p for p in d.iterdir()
                  if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in MEDIA_EXTS)


def quick_fp(path: Path, size: int | None = None) -> str:
    """크기 + 앞뒤 QUICK 바이트의 sha1. 2*QUICK 이하 파일은 전체를 읽는다.
    `index.fingerprint`(앞뒤 1MB)와 다르다 — 그쪽은 보관본(빠른 디스크)용이고 이쪽은 느린 녹음기를 훑는 용도다."""
    size = path.stat().st_size if size is None else size
    h = hashlib.sha1(str(size).encode())
    with open(path, "rb") as f:
        if size <= 2 * QUICK:
            h.update(f.read())
        else:
            h.update(f.read(QUICK))
            f.seek(size - QUICK)
            h.update(f.read(QUICK))
    return h.hexdigest()


def full_sha1(path: Path, progress: str | None = None) -> str:
    h = hashlib.sha1()
    _stream(path, h.update, progress)
    return h.hexdigest()


def _stream(path: Path, sink, progress: str | None = None) -> int:
    """path를 CHUNK씩 읽어 sink에 넘긴다. progress가 있으면 PROGRESS_SEC마다 속도와 남은 시간을 남긴다."""
    total = path.stat().st_size
    done = 0
    t0 = last = time.monotonic()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            sink(chunk)
            done += len(chunk)
            now = time.monotonic()
            if progress and now - last >= PROGRESS_SEC and done < total:
                rate = done / (now - t0)
                log(f"{progress} {done / 2**20:.0f}/{total / 2**20:.0f}MB · {rate / 2**20:.2f}MB/s · "
                    f"남은 시간 약 {(total - done) / rate / 60:.0f}분")
                last = now
    return done


# ---- 매니페스트: {"files": {"<크기>:<sha1>": {name, source_name, size, sha1, quick, ingested_at, moved_to?}}}

def fp_key(size: int, sha1: str) -> str:
    return f"{size}:{sha1}"


def load_manifest(archive: Path) -> dict:
    p = archive / MANIFEST
    if not p.is_file():
        return {"version": 1, "files": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise SystemExit(f"매니페스트를 읽을 수 없음: {p} — 고치거나 옮긴 뒤 다시 실행") from None
    data.setdefault("files", {})
    return data


def save_manifest(archive: Path, data: dict) -> None:
    """중간에 끊겨도 깨진 파일이 남지 않게 임시 파일에 쓰고 바꿔 끼운다."""
    fd, tmp = tempfile.mkstemp(dir=archive, prefix=".rec2md-ingest-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, archive / MANIFEST)


# ---- 계획

@dataclass
class Entry:
    src: Path
    size: int
    quick: str
    kind: str                 # NEW / KNOWN / RENAMED
    dest: str                 # 보관 폴더 안 이름 (KNOWN이면 이미 있는 이름)
    key: str | None = None    # KNOWN이면 매니페스트 키
    sha1: str | None = None   # 이번 실행에서 녹음기 파일 전체를 읽어 얻은 sha1
    error: str | None = None
    notes: list[str] = field(default_factory=list)


def plan(recorder: Path, archive: Path, manifest: dict) -> list[Entry]:
    """녹음기 파일마다 이미 받았는지, 새로 받는다면 보관 이름을 무엇으로 할지 정한다. 아무것도 쓰지 않는다."""
    files = manifest["files"]
    by_quick = {(r["size"], r.get("quick")): k for k, r in files.items()}
    # 보관 이름 예약: 디스크에 있는 이름 + 매니페스트가 기억하는 이름(사람이 옮겨도 다시 쓰지 않는다). 대소문자 무시
    taken = {p.name.casefold() for p in archive.iterdir()} if archive.is_dir() else set()
    taken |= {n.casefold() for r in files.values() for n in [r["name"], *r.get("previous_names", [])]}
    out = []
    for f in media_files(recorder):
        size = f.stat().st_size
        q = quick_fp(f, size)
        key = by_quick.get((size, q))
        if key:
            rec = files[key]
            kept = archive / rec["name"]
            if kept.is_file() and kept.stat().st_size == rec["size"]:
                e = Entry(f, size, q, KNOWN, rec["name"], key=key)
                if rec.get("source_name") != f.name:
                    e.notes.append(f"예전 녹음기 이름 {rec.get('source_name')}")
                out.append(e)
                continue
            # 매니페스트에는 있는데 보관본이 사라졌거나 크기가 다르다 → 녹음기에 원본이 남아 있으니 다시 받는다.
            # 그 이름이 비어 있으면 같은 이름으로, 무언가 있으면 덮어쓰지 않고 새 이름으로
            dest = rec["name"] if not kept.exists() else _free_name(f.name, taken)
            taken.add(dest.casefold())
            e = Entry(f, size, q, RESTORE, dest)
            e.notes.append(f"보관본 {rec['name']}가 {'없음' if not kept.exists() else '크기가 다름'}")
            out.append(e)
            continue
        dest = _free_name(f.name, taken)
        taken.add(dest.casefold())
        out.append(Entry(f, size, q, NEW if dest == f.name else RENAMED, dest))
    return out


def _free_name(name: str, taken: set[str]) -> str:
    if name.casefold() not in taken and f"{name}.part".casefold() not in taken:
        return name
    stem, ext = os.path.splitext(name)
    n = 2
    while f"{stem}-{n}{ext}".casefold() in taken:
        n += 1
    return f"{stem}-{n}{ext}"


def adopt_existing(entries: list[Entry], archive: Path, manifest: dict) -> None:
    """매니페스트 없이 손으로 먼저 복사해 둔 파일: 보관 폴더의 같은 이름 파일이 크기·빠른 지문까지 같으면
    보관본 전체 sha1(보관 디스크라 빠름)로 매니페스트에 올리고 이미 받은 것으로 본다."""
    for e in entries:
        if e.kind != RENAMED:
            continue
        have = archive / e.src.name
        if not have.is_file() or have.stat().st_size != e.size or quick_fp(have) != e.quick:
            continue
        if any(r["name"].casefold() == have.name.casefold() for r in manifest["files"].values()):
            continue  # 매니페스트가 아는 다른 녹음이다(빠른 지문이 같을 리는 없지만 덮어쓰지 않는다)
        sha1 = full_sha1(have)
        key = fp_key(e.size, sha1)
        manifest["files"].setdefault(key, {"name": have.name, "source_name": e.src.name, "size": e.size,
                                           "sha1": sha1, "quick": e.quick, "ingested_at": _now(),
                                           "adopted": True})
        e.kind, e.dest, e.key = KNOWN, have.name, key
        e.notes.append("보관 폴더에 먼저 있던 사본을 매니페스트에 올림")


def free_space_problem(entries: list[Entry], archive: Path) -> str | None:
    need = sum(e.size for e in entries if e.kind in COPY)
    if not need:
        return None
    free = shutil.disk_usage(archive).free
    if free - need < SPACE_MARGIN:
        return (f"보관 디스크 공간 부족: 새 파일 {need / 2**30:.1f}GB + 여유 {SPACE_MARGIN / 2**30:.0f}GB 필요, "
                f"남은 공간 {free / 2**30:.1f}GB")
    return None


# ---- 복사

def copy_in(e: Entry, archive: Path, manifest: dict) -> None:
    """녹음기 파일을 `<보관 이름>.part`에 쓰며 sha1을 계산하고, 보관본을 다시 읽어 같을 때만 이름을 바꾼다.
    최종 이름이 그새 생겼으면 덮어쓰지 않고 실패로 남긴다."""
    final = archive / e.dest
    part = archive / f"{e.dest}.part"
    h = hashlib.sha1()
    with open(part, "wb") as out:   # .part는 이 명령만 쓰는 임시 이름이라 이전 실행의 잔여물은 덮어쓴다
        def sink(chunk: bytes) -> None:
            h.update(chunk)
            out.write(chunk)
        n = _stream(e.src, sink, progress=f"복사 {e.src.name}")
        out.flush()
        os.fsync(out.fileno())
    sha1 = h.hexdigest()
    if n != e.size:
        part.unlink()
        raise OSError(f"읽은 크기가 다름: {n} ≠ {e.size} (복사 중 파일이 바뀜?)")
    if full_sha1(part) != sha1:
        part.unlink()
        raise OSError("보관본 sha1이 읽은 내용과 다름 (쓰기 오류)")
    if final.exists():
        raise FileExistsError(f"보관 폴더에 {e.dest}가 그새 생겨 덮어쓰지 않음 ({part.name}는 남겨 둠)")
    os.rename(part, final)
    e.sha1, e.key = sha1, fp_key(e.size, sha1)
    new = {"name": e.dest, "source_name": e.src.name, "size": e.size, "sha1": sha1, "quick": e.quick,
           "ingested_at": _now()}
    old = manifest["files"].get(e.key)
    if old:   # 보관본을 다시 받음: 예전 이름은 계속 예약하고 이동 기록은 남긴다
        prev = [*old.get("previous_names", []), *([old["name"]] if old["name"] != e.dest else [])]
        if prev:
            new["previous_names"] = prev
        if old.get("moved_to"):
            new["moved_to"] = old["moved_to"]
    manifest["files"][e.key] = new


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---- 처리완료 폴더로 옮기기

def transcribed(out: Path, name: str) -> bool:
    """batch 상태 파일에서 done이고 segments.json이 있는가. `rec2md index`가 출력 이름을 바꿨으면 그 이름으로 본다."""
    state_path = out / ".rec2md-state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    rec = state.get(name)
    if not isinstance(rec, dict) or rec.get("status") != "done":
        return False
    stem = rec.get("outputs") or Path(name).stem
    return (out / f"{stem}.segments.json").is_file()


def same_device(a: Path, b: Path) -> bool:
    return os.stat(a).st_dev == os.stat(b).st_dev


def move_check(e: Entry, archive: Path, manifest: dict, out: Path, dest_dir: Path) -> str | None:
    """옮기면 안 되는 이유. None이면 옮겨도 된다(녹음기 파일 sha1 확인은 move_out에서)."""
    if e.kind != KNOWN or not e.key:
        return "보관 폴더에 받지 못함"
    rec = manifest["files"][e.key]
    kept = archive / rec["name"]
    if not kept.is_file() or kept.stat().st_size != rec["size"]:
        return f"보관본 {rec['name']}가 없거나 크기가 다름"
    if not transcribed(out, rec["name"]):
        return "전사가 끝나지 않음(상태 done과 segments.json 둘 다 필요)"
    if (dest_dir / e.src.name).exists():
        return "처리완료 폴더에 같은 이름이 있음"
    return None


def move_out(e: Entry, archive: Path, manifest: dict, dest_dir: Path) -> None:
    """녹음기 안에서만 옮긴다(같은 디스크라 이름 바꾸기). 보관본과 녹음기 파일의 sha1이 모두 매니페스트와 같아야 한다."""
    rec = manifest["files"][e.key]
    if full_sha1(archive / rec["name"]) != rec["sha1"]:   # 보관 디스크라 빠르다
        raise ValueError(f"보관본 {rec['name']}의 sha1이 매니페스트와 다름")
    sha1 = e.sha1 or full_sha1(e.src, progress=f"sha1 확인 {e.src.name}")
    if sha1 != rec["sha1"] or e.src.stat().st_size != rec["size"]:
        raise ValueError("녹음기 파일 sha1이 보관본과 다름")
    target = dest_dir / e.src.name
    if target.exists():
        raise FileExistsError(f"처리완료 폴더에 {e.src.name}가 있음")
    os.rename(e.src, target)
    rec.setdefault("moved_to", []).append(str(target))
