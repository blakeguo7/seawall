import pytest

from greet import main


def run(argv, capsys):
    code = main(argv)
    return code, capsys.readouterr().out


def test_plain(capsys):
    assert run(["Ada"], capsys) == (0, "Hello, Ada!\n")


@pytest.mark.parametrize("flag", ["--shout", "-s"])
def test_shout(flag, capsys):
    assert run(["Ada", flag], capsys) == (0, "HELLO, ADA!\n")


@pytest.mark.parametrize("flag", ["--times", "-n"])
def test_times(flag, capsys):
    assert run(["Ada", flag, "3"], capsys) == (0, "Hello, Ada!\n" * 3)


def test_times_and_shout(capsys):
    assert run(["Ada", "-s", "-n", "2"], capsys) == (0, "HELLO, ADA!\n" * 2)


@pytest.mark.parametrize("value", ["0", "-2", "abc", "1.5"])
def test_times_must_be_a_positive_integer(value, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["Ada", "--times", value])
    assert excinfo.value.code == 2


@pytest.mark.parametrize("flag", ["--output", "-o"])
def test_output_file(flag, tmp_path, capsys):
    target = tmp_path / "out.txt"
    target.write_text("old contents")
    assert run(["Ada", flag, str(target), "-n", "2"], capsys) == (0, "")
    assert target.read_text() == "Hello, Ada!\nHello, Ada!\n"


def test_name_is_still_required(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


def test_new_options_have_tests():
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "tests" / "test_greet.py").read_text()
    assert text.count("def test_") >= 4
    for option in ("--shout", "--times", "--output"):
        assert option in text or option[1:3] in text
