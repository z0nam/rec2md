"""화자 이름 계약 (`meta.speaker_names`). numpy 없이 가벼워서 voices·review·render가 같이 쓴다.

    meta.speaker_names = {"화자3": {"name": "홍길동", "score": 0.81[, "tentative": true][, "same_as": "화자1"]},
                          "화자5": {"name": "김철수", "source": "user"}}

- 자동 매칭(voices.match)은 name·score를 쓰고, 불확실하면 tentative, 쪼개진 같은 사람이면 same_as를 단다.
- 사람이 정한 이름은 `{"name", "source": "user"}`(score 없음). `voices enroll --from-names`는 이것만 등록하고,
  자동 매칭은 이것을 덮어쓰지 않는다. 사람이 정한 이름은 반드시 `user_entry()`로 만든다.
"""

from __future__ import annotations

USER = "user"


def user_entry(name: str) -> dict:
    return {"name": name, "source": USER}


def is_user(d) -> bool:
    return isinstance(d, dict) and d.get("source") == USER and bool(d.get("name"))


def user_names(t) -> dict[str, str]:
    """사람이 정한 이름만 {화자N: 이름}으로."""
    return {lb: d["name"] for lb, d in (t.meta.get("speaker_names") or {}).items() if is_user(d)}


def user_label_for(names: dict, name: str) -> str | None:
    """그 이름을 사람이 이미 붙인 라벨."""
    return next((lb for lb, d in names.items() if is_user(d) and d["name"] == name), None)


def set_user_names(names: dict, mapping: dict[str, str], unset=()) -> dict:
    """names에서 unset 라벨을 지우고 mapping {라벨: 이름}을 사람이 정한 이름으로 넣은 새 dict."""
    out = {k: v for k, v in names.items() if k not in set(unset)}
    out.update({lb: user_entry(nm) for lb, nm in mapping.items()})
    return out


def display_name(label: str | None, names: dict) -> str:
    """화면에 보일 이름. 자동 매칭이 불확실하면 "이름?", 사람이 정한 이름은 ? 없음, 이름이 없으면 라벨."""
    if not label:
        return ""
    d = names.get(label)
    if isinstance(d, dict) and d.get("name"):
        return d["name"] + ("?" if d.get("tentative") and not is_user(d) else "")
    return label
