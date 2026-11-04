"""speechcheck.windows: 남긴 구간 위에 표본 창을 고르게 놓는다(모델 없이)."""

from rec2md import speechcheck


def test_windows_spread_over_kept_regions_and_stay_inside():
    keep = [(0.0, 600.0), (3600.0, 4200.0)]   # 10분 + 10분, 사이 50분은 잘라냄
    ws = speechcheck.windows(keep, n=4, win=30.0)
    assert len(ws) == 4
    assert sum(1 for s, _ in ws if s < 600) == 2 and sum(1 for s, _ in ws if s >= 3600) == 2
    for s, d in ws:
        assert d == 30.0
        assert any(a <= s and s + d <= b for a, b in keep)


def test_windows_clamp_to_short_region_and_edges():
    ws = speechcheck.windows([(100.0, 110.0)], n=3, win=30.0)
    assert ws == [(100.0, 10.0)] * 3
    ws = speechcheck.windows([(0.0, 3600.0)], n=1, win=30.0)
    assert ws == [(1785.0, 30.0)]
    assert speechcheck.windows([], n=5) == []
