import pytest

from greet import main


def test_plain_greeting(capsys):
    assert main(["Ada"]) == 0
    assert capsys.readouterr().out == "Hello, Ada!\n"


def test_shout(capsys):
    main(["Ada", "--shout"])
    assert capsys.readouterr().out == "HELLO, ADA!\n"


def test_times(capsys):
    main(["Ada", "-n", "2"])
    assert capsys.readouterr().out == "Hello, Ada!\n" * 2


def test_times_must_be_positive():
    with pytest.raises(SystemExit):
        main(["Ada", "--times", "0"])


def test_output_file(tmp_path, capsys):
    target = tmp_path / "out.txt"
    main(["Ada", "-o", str(target)])
    assert target.read_text() == "Hello, Ada!\n"
    assert capsys.readouterr().out == ""
