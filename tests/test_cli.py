"""cli 모듈 단위 테스트: 미구현 서브커맨드 SystemExit 등 검증."""

import pytest
from rec2md.cli import main


def test_unimplemented_subcommand_voices():
    with pytest.raises(SystemExit) as exc_info:
        main(["voices"])
    assert "아직 구현되지 않은 명령: voices" in str(exc_info.value)


def test_unimplemented_subcommand(monkeypatch):
    # 예약 명령은 하나씩 구현되므로 실제 이름 대신 가짜 예약 명령으로 확인한다
    import rec2md.cli
    monkeypatch.setattr(rec2md.cli, "SUBCOMMANDS", rec2md.cli.SUBCOMMANDS | {"nosuchcmd"})
    with pytest.raises(SystemExit) as exc_info:
        main(["nosuchcmd"])
    assert "아직 구현되지 않은 명령: nosuchcmd" in str(exc_info.value)
