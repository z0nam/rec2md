"""cli 모듈 단위 테스트: 미구현 서브커맨드 SystemExit 등 검증."""

import pytest
from rec2md.cli import main


def test_unimplemented_subcommand_voices():
    with pytest.raises(SystemExit) as exc_info:
        main(["voices"])
    assert "아직 구현되지 않은 명령: voices" in str(exc_info.value)


def test_unimplemented_subcommand_render():
    with pytest.raises(SystemExit) as exc_info:
        main(["render"])
    assert "아직 구현되지 않은 명령: render" in str(exc_info.value)
