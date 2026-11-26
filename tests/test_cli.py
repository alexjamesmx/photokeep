from photokeep.cli import main


def test_version_flag(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "photokeep 0.1.0"
