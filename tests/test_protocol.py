"""figtracer/protocol.py is a shim: the protocol system lives in protokit now."""
from figtracer import protocol


def test_forwards_to_protokit_when_installed(monkeypatch):
    seen = {}
    monkeypatch.setattr(protocol.shutil, "which", lambda x: "/bin/protokit")
    monkeypatch.setattr(protocol.subprocess, "call", lambda cmd: seen.setdefault("cmd", cmd) and 0)
    assert protocol.main(["--dir", "/exp"]) == 0
    assert seen["cmd"] == ["/bin/protokit", "--dir", "/exp"]


def test_says_where_it_went_when_not_installed(monkeypatch, capsys):
    monkeypatch.setattr(protocol.shutil, "which", lambda x: None)
    assert protocol.main(["--dir", "/exp"]) == 2
    assert "protokit" in capsys.readouterr().err
