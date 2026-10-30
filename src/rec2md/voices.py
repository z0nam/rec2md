"""화자 목소리 DB: 클러스터 임베딩을 실명에 등록하고, 새 녹취의 화자에 실명을 붙인다.

- 사이드카 `<이름>.voices.npz`: 전사 때 화자N → 임베딩, 발화 초. 녹취록을 확정한 뒤 다시 돌리지 않고 등록하려고 남긴다.
- DB `~/.config/rec2md/voices/voices.db`(sqlite, 권한 600): 사람마다 임베딩 여러 개(녹취별). 생체정보다.
- 매칭: 사람별 최대 코사인 → 헝가리안 1:1 → 임계값 미만은 이름을 붙이지 않는다(틀린 이름이 이름 없음보다 해롭다).
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from .render import fmt_ts
from .segments import Transcript

DB_PATH = Path(os.environ.get("REC2MD_VOICES_DB", Path.home() / ".config" / "rec2md" / "voices" / "voices.db"))
THRESHOLD = 0.75  # eval_voices 실측(2026-10-30, 녹취 5건): 같은 사람 최저 0.85, 다른 사람 최고 0.66
MARGIN = 0.05  # 임계값 + 이 값 미만은 "이름?"으로 표시
MIN_ENROLL_SEC = 30.0  # 이보다 짧은 클러스터는 임베딩이 불안정해 등록을 거절한다
OUTLIER_COS = 0.5  # 구간 임베딩이 클러스터 중심과 이보다 멀면 다른 사람이 섞인 것으로 보고 등록에서 뺀다
MIN_PURITY = 0.8  # 등록에 남은 발화 비율이 이보다 낮으면 경고
# 실측(녹취 5건): 같은 사람의 녹취 간 등록 벡터 최저 0.83, 다른 사람끼리 최고 0.65
ENROLL_MIN_SIM = 0.75  # 이미 등록된 사람에 새 임베딩을 더할 때, 기존 것과 이만큼 닮지 않으면 거절(--force로 강제)
# 실측: 맞게 붙은 화자의 1위-2위 사람 점수 차 최저 0.26. 차가 이보다 작으면 두 사람 사이에서 헷갈린 것으로 본다
MIN_GAP = 0.15


def sidecar_path(seg_path: Path) -> Path:
    name = seg_path.name.removesuffix(".segments.json")
    return seg_path.with_name(f"{name}.voices.npz")


def save_sidecar(path: Path, embs: dict, turns: list, rename: dict[str, str], segs: list = ()) -> None:
    """embs·turns·segs는 pyannote 원래 라벨, rename은 원래 라벨 → 화자N(assign_speakers가 채움).

    segs [(start, end, 라벨, 벡터)]는 구간별 임베딩(강건 등록용). 없으면 클러스터 중심만으로 등록한다.
    """
    secs: dict[str, float] = {}
    for s, e, spk in turns:
        secs[spk] = secs.get(spk, 0.0) + (e - s)
    raw = [r for r in rename if r in embs]  # 전사 단어가 하나도 안 붙은 클러스터는 화자N이 없으니 뺀다
    dim = len(next(iter(embs.values()), ()))
    seg = [x for x in segs if x[2] in rename]
    # 생체정보라 처음부터 본인만 읽게 만들고, 기존 파일을 덮어쓸 때도 권한을 다시 좁힌다
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as f:
        np.savez(f, labels=np.array([rename[r] for r in raw], dtype=str),
                 vecs=np.array([embs[r] for r in raw], dtype=np.float32).reshape(len(raw), dim),
                 seconds=np.array([secs.get(r, 0.0) for r in raw], dtype=np.float32),
                 seg_labels=np.array([rename[x[2]] for x in seg], dtype=str),
                 seg_start=np.array([x[0] for x in seg], dtype=np.float32),
                 seg_end=np.array([x[1] for x in seg], dtype=np.float32),
                 seg_vecs=np.array([x[3] for x in seg], dtype=np.float32).reshape(len(seg), dim))


def load_segments(path: Path) -> dict[str, list[tuple[float, float, np.ndarray]]]:
    """화자N → [(start, end, 벡터)]. 구간 임베딩이 없는 옛 사이드카면 빈 dict."""
    out: dict[str, list] = {}
    with np.load(path) as z:
        if "seg_vecs" not in z.files:
            return out
        for lb, s, e, v in zip(z["seg_labels"], z["seg_start"], z["seg_end"], z["seg_vecs"]):
            out.setdefault(str(lb), []).append((float(s), float(e), v))
    return out


def robust_vector(center: np.ndarray, segs: list[tuple[float, float, np.ndarray]],
                  outlier_cos: float = OUTLIER_COS) -> tuple[np.ndarray, float, list[tuple[float, float]]]:
    """중심에서 먼 구간을 뺀 구간 임베딩 평균, 남은 발화 비율(순도), 뺀 구간 시각.

    구간이 3개 미만이면 판단할 근거가 없으니 중심을 그대로 쓴다(순도 1).
    """
    if len(segs) < 3:
        return _unit(center), 1.0, []
    c = _unit(center)
    vs = np.stack([_unit(v) for _, _, v in segs])
    dur = np.array([e - s for s, e, _ in segs])
    keep = vs @ c >= outlier_cos
    if not keep.any():
        return c, 0.0, [(s, e) for s, e, _ in segs]
    purity = float(dur[keep].sum() / dur.sum())
    return _unit(vs[keep].mean(axis=0)), purity, [(s, e) for (s, e, _), k in zip(segs, keep) if not k]


def load_sidecar(path: Path) -> dict[str, tuple[np.ndarray, float]]:
    if not path.is_file():
        raise SystemExit(f"목소리 사이드카 없음: {path} (화자분리를 켜고 다시 전사해야 생김)")
    with np.load(path) as z:
        return {str(lb): (v, float(sec)) for lb, v, sec in zip(z["labels"], z["vecs"], z["seconds"])}


def _unit(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).ravel()
    return v / max(float(np.linalg.norm(v)), 1e-9)


class VoiceDB:
    def __init__(self, path: Path | str = DB_PATH):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        old = os.umask(0o077)  # 생체정보: 새로 만드는 DB·저널 파일을 본인만 읽게
        try:
            self.con = sqlite3.connect(path)
        finally:
            os.umask(old)
        if path != ":memory:":
            os.chmod(path, 0o600)
        self.con.executescript("""
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS person(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, note TEXT);
            CREATE TABLE IF NOT EXISTS embedding(
                id INTEGER PRIMARY KEY,
                person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
                vec BLOB NOT NULL, source TEXT, label TEXT, seconds REAL, created REAL,
                UNIQUE(person_id, source, label));
        """)

    @classmethod
    @contextmanager
    def open(cls, path: Path | str = DB_PATH):
        db = cls(path)
        try:
            yield db
            db.con.commit()
        finally:
            db.con.close()

    def add(self, name: str, vec: np.ndarray, source: str, label: str, seconds: float) -> bool:
        """같은 녹취의 같은 클러스터를 두 번 등록하면 무시하고 False."""
        self.con.execute("INSERT OR IGNORE INTO person(name) VALUES (?)", (name,))
        pid = self.con.execute("SELECT id FROM person WHERE name = ?", (name,)).fetchone()[0]
        cur = self.con.execute(
            "INSERT OR IGNORE INTO embedding(person_id, vec, source, label, seconds, created) VALUES (?,?,?,?,?,?)",
            (pid, _unit(vec).tobytes(), source, label, seconds, time.time()))
        return cur.rowcount > 0

    def people(self) -> list[tuple[str, int, float]]:
        """(이름, 임베딩 수, 등록된 발화 초 합)"""
        return self.con.execute(
            "SELECT p.name, COUNT(e.id), COALESCE(SUM(e.seconds), 0) FROM person p "
            "LEFT JOIN embedding e ON e.person_id = p.id GROUP BY p.id ORDER BY p.name").fetchall()

    def rename(self, old: str, new: str) -> None:
        if self.con.execute("UPDATE person SET name = ? WHERE name = ?", (new, old)).rowcount == 0:
            raise SystemExit(f"등록되지 않은 이름: {old}")

    def forget(self, name: str) -> int:
        n = self.con.execute("SELECT COUNT(*) FROM embedding e JOIN person p ON p.id = e.person_id "
                             "WHERE p.name = ?", (name,)).fetchone()[0]
        if self.con.execute("DELETE FROM person WHERE name = ?", (name,)).rowcount == 0:
            raise SystemExit(f"등록되지 않은 이름: {name}")
        return n

    def vectors(self) -> dict[str, np.ndarray]:
        """이름 → (임베딩 수, 차원) 행렬(단위 벡터)."""
        out: dict[str, list[np.ndarray]] = {}
        for name, blob in self.con.execute(
                "SELECT p.name, e.vec FROM embedding e JOIN person p ON p.id = e.person_id"):
            out.setdefault(name, []).append(np.frombuffer(blob, dtype=np.float32))
        return {k: np.stack(v) for k, v in out.items()}


def user_names(t: Transcript) -> dict[str, str]:
    """meta.speaker_names 중 사용자가 지정한 것(source == "user")을 {화자N: 이름}으로."""
    return {lb: d["name"] for lb, d in (t.meta.get("speaker_names") or {}).items()
            if isinstance(d, dict) and d.get("source") == "user" and d.get("name")}


def enroll(db: VoiceDB, sidecar: dict[str, tuple[np.ndarray, float]], mapping: dict[str, str],
           source: str, min_sec: float = MIN_ENROLL_SEC, skip_short: bool = False,
           segments: dict[str, list] | None = None, force: bool = False,
           min_sim: float = ENROLL_MIN_SIM) -> list[str]:
    """mapping {화자N: 이름}을 등록하고 결과 문장 목록을 돌려준다.

    짧은 클러스터, 그리고 이미 등록된 그 사람의 목소리와 닮지 않은 클러스터(이름을 잘못 줬거나 원거리 녹음처럼
    방·마이크가 크게 다른 경우)는 기본으로 전부 거절한다. skip_short면 그것만 빼고 등록한다(검토 화면 이름 일괄 등록용).
    force면 닮음 검사를 건너뛴다. 처음 등록하는 사람은 비교할 것이 없으니 검사하지 않는다.
    segments(구간 임베딩)가 있으면 중심에서 먼 구간을 뺀 벡터를 등록하고, 많이 빠졌으면 경고한다.
    """
    for label in mapping:
        if label not in sidecar:
            raise SystemExit(f"사이드카에 없는 화자: {label} (있는 화자: {', '.join(sidecar)})")
    short = [lb for lb in mapping if sidecar[lb][1] < min_sec]
    if short and not skip_short:
        raise SystemExit(f"발화가 {min_sec:.0f}초 미만이라 등록하지 않음: "
                         + ", ".join(f"{lb}({sidecar[lb][1]:.0f}초)" for lb in short))
    msgs = [f"{lb} → {mapping[lb]} 건너뜀: 발화 {sidecar[lb][1]:.0f}초 < {min_sec:.0f}초" for lb in short]
    mapping = {lb: n for lb, n in mapping.items() if lb not in short}
    robust = {lb: robust_vector(sidecar[lb][0], (segments or {}).get(lb, [])) for lb in mapping}
    known = db.vectors()
    unlike = {lb: float((known[n] @ robust[lb][0]).max()) for lb, n in mapping.items()
              if n in known and not force}
    unlike = {lb: sim for lb, sim in unlike.items() if sim < min_sim}
    if unlike and not skip_short:
        raise SystemExit("이미 등록된 목소리와 닮지 않아 등록하지 않음(이름이 맞는지 확인, 원거리 녹음 등 확실하면 --force): "
                         + ", ".join(f"{lb}→{mapping[lb]} 유사도 {sim:.2f} < {min_sim:.2f}" for lb, sim in unlike.items()))
    msgs += [f"{lb} → {mapping[lb]} 건너뜀: 등록된 목소리와 유사도 {sim:.2f} < {min_sim:.2f} (확실하면 --force)"
             for lb, sim in unlike.items()]
    mapping = {lb: n for lb, n in mapping.items() if lb not in unlike}
    for label, name in mapping.items():
        sec = sidecar[label][1]
        vec, purity, dropped = robust[label]
        added = db.add(name, vec, source, label, sec)
        msgs.append(f"{label} → {name} ({sec:.0f}초)" + ("" if added else " — 이미 등록됨"))
        # 짧은 구간은 같은 사람이어도 중심에서 멀게 나오곤 해서(실측 순도 0.88 이상) 순도가 낮을 때만 알린다
        if added and purity < MIN_PURITY:
            when = ", ".join(f"{fmt_ts(s)}〜{fmt_ts(e)}" for s, e in dropped[:8]) + (" …" if len(dropped) > 8 else "")
            msgs.append(f"  경고: 다른 사람이 섞였을 수 있음. 순도 {purity:.0%}, "
                        f"중심에서 먼 구간 {len(dropped)}개는 빼고 등록: {when}")
    return msgs


def score_matrix(people: dict[str, np.ndarray], clusters: dict[str, tuple[np.ndarray, float]]
                 ) -> tuple[list[str], list[str], np.ndarray]:
    """(화자 라벨, 이름, [화자 × 이름] 사람별 최대 코사인)"""
    labels, names = list(clusters), list(people)
    m = np.zeros((len(labels), len(names)), dtype=np.float32)
    for i, lb in enumerate(labels):
        q = _unit(clusters[lb][0])
        for j, nm in enumerate(names):
            m[i, j] = float((people[nm] @ q).max())
    return labels, names, m


def match(people: dict[str, np.ndarray], clusters: dict[str, tuple[np.ndarray, float]],
          threshold: float = THRESHOLD, margin: float = MARGIN, min_gap: float = MIN_GAP) -> dict[str, dict]:
    """{화자N: {"name", "score"[, "tentative"][, "gap"][, "same_as"]}}. 임계값 미만은 빠진다.

    한 녹취에서 한 사람은 한 클러스터에만 붙인다(헝가리안). pyannote가 한 사람을 둘로 쪼갠 경우를 위해,
    1:1에서 밀렸지만 같은 사람에 임계값을 넘는 클러스터는 same_as를 달고 tentative로 남긴다.
    붙은 사람과 다음으로 닮은 사람의 점수 차가 min_gap보다 작으면 tentative로 낮추고 그 차를 gap에 남긴다.
    """
    if not people or not clusters:
        return {}
    from scipy.optimize import linear_sum_assignment

    labels, names, m = score_matrix(people, clusters)
    # 임계값 미만 짝은 0점으로 둬서, 쓰이지도 않을 짝이 유효한 짝을 밀어내지 못하게 한다
    rows, cols = linear_sum_assignment(-np.where(m >= threshold, m, 0.0))
    groups: dict[str, list[int]] = {}  # 이름 → 그 이름에 붙을 클러스터 행들
    for i, j in zip(rows, cols):
        if m[i, j] >= threshold:
            groups.setdefault(names[j], []).append(i)
    for i in set(range(len(labels))) - set(rows[m[rows, cols] >= threshold]):
        j = int(m[i].argmax())
        if m[i, j] >= threshold and names[j] in groups:
            groups[names[j]].append(i)
    out: dict[str, dict] = {}
    for name, rows_ in groups.items():
        j = names.index(name)
        main = max(rows_, key=lambda i: clusters[labels[i]][1])  # 쪼개졌으면 발화가 긴 쪽을 본 화자로
        out[labels[main]] = _hit(name, m[main, j], threshold, margin)
        others = np.delete(m[main], j)
        if len(others) and (gap := float(m[main, j] - others.max())) < min_gap:
            out[labels[main]].update(tentative=True, gap=round(gap, 3))
        for i in rows_:
            if i != main:
                out[labels[i]] = {"name": name, "score": round(float(m[i, j]), 3), "tentative": True,
                                  "same_as": labels[main]}
    return out


def _hit(name: str, score: float, threshold: float, margin: float) -> dict:
    d = {"name": name, "score": round(float(score), 3)}
    if score < threshold + margin:
        d["tentative"] = True
    return d


def annotate(t: Transcript, sidecar: Path, db_path: Path | str = DB_PATH, threshold: float = THRESHOLD) -> int:
    """DB가 있으면 t.meta["speaker_names"]를 채우고 자동으로 붙인 이름 수를 돌려준다. DB가 없으면 0.

    사용자가 지정한 이름(source "user")은 그대로 두고, 그 화자와 그 사람은 자동 매칭에서 뺀다.
    """
    if db_path != ":memory:" and not Path(db_path).is_file():
        return 0
    with VoiceDB.open(db_path) as db:
        people = db.vectors()
    fixed = {lb: d for lb, d in (t.meta.get("speaker_names") or {}).items()
             if isinstance(d, dict) and d.get("source") == "user"}
    taken = {d.get("name") for d in fixed.values()}
    clusters = {lb: v for lb, v in load_sidecar(sidecar).items() if lb not in fixed}
    names = match({n: v for n, v in people.items() if n not in taken}, clusters, threshold)
    t.meta["speaker_names"] = {**names, **fixed}
    t.meta["voice_threshold"] = threshold
    return len(names)
