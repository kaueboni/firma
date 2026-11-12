from typer.testing import CliRunner

from firma import __version__
from firma.cli import app


def test_versao():
    r = CliRunner().invoke(app, ["--version"])
    assert r.exit_code == 0
    assert __version__ in r.output


def test_doctor_lista_checagens():
    r = CliRunner().invoke(app, ["doctor"])
    assert "Python" in r.output
    assert "Credencial" in r.output
