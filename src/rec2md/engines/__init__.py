from __future__ import annotations

from .base import Engine


def get_engine(name: str, **kw) -> Engine:
    if name == "local":
        from .local_mlx import LocalMLX
        return LocalMLX(**kw)
    raise SystemExit(f"아직 없는 엔진: {name} (현재: local)")
