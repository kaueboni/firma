import subprocess
import sys

import pytest
from typer.testing import CliRunner

from firma import projeto, registro
from firma.cli import app

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


@pytest.fixture(autouse=True)
def firma_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FIRMA_HOME", str(tmp_path / "home-firma"))


def _repo(pasta, remoto="git@github.com:kaueboni/app.git"):
    subprocess.run(["git", "init", "-q", "-b", "main", str(pasta)], check=True)
    if remoto:
        subprocess.run(["git", "-C", str(pasta), "remote", "add", "origin", remoto], check=True)
    return pasta


@pytest.mark.parametrize(
    "url,esperado",
    [
        ("git@github.com:kaueboni/firma.git", "kaueboni/firma"),
        ("https://github.com/kaueboni/firma", "kaueboni/firma"),
        ("https://github.com/kaueboni/firma.git/", "kaueboni/firma"),
        ("https://gitlab.com/x/y.git", None),
        (None, None),
    ],
)
def test_parse_github(url, esperado):
    assert projeto.parse_github(url) == esperado


def test_python_com_uv_pytest_e_ruff(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\n[tool.ruff]\nline-length=100\n[tool.pytest.ini_options]\n')
    (tmp_path / "uv.lock").write_text("")
    assert projeto.detectar_comandos(tmp_path) == ("uv run pytest -q", "uv run ruff check .")


def test_python_com_requirements(tmp_path):
    (tmp_path / "requirements-dev.txt").write_text("pytest>=8\nruff\n")
    assert projeto.detectar_comandos(tmp_path) == ("pytest -q", "ruff check .")


def test_node_com_pnpm(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest", "lint": "eslint ."}}')
    (tmp_path / "pnpm-lock.yaml").write_text("")
    assert projeto.detectar_comandos(tmp_path) == ("pnpm test", "pnpm run lint")


def test_makefile_com_alvo_test(tmp_path):
    (tmp_path / "Makefile").write_text("build:\n\techo\ntest:\n\techo ok\n")
    assert projeto.detectar_comandos(tmp_path) == ("make test", None)


def test_sem_nada_avisa(tmp_path):
    d = projeto.detectar(_repo(tmp_path, remoto=None))
    assert d.cmd_testes is None and d.github is None
    assert len(d.avisos) == 2


def test_fora_de_repositorio(tmp_path):
    assert projeto.detectar(tmp_path) is None


def test_config_gerado_e_toml_valido(tmp_path):
    d = projeto.Deteccao(raiz=tmp_path, nome='app "x"', branch_principal="main", cmd_testes="pytest -q")
    dados = tomllib.loads(projeto.gerar_config(d))
    assert dados["projeto"]["nome"] == 'app "x"'
    assert dados["comandos"]["testes"] == "pytest -q"
    assert dados["quota"]["teto_semanal"] == 0.6


def test_gitignore_acrescenta_uma_vez(tmp_path):
    (tmp_path / ".gitignore").write_text("node_modules")
    assert projeto.garantir_gitignore(tmp_path) is True
    assert projeto.garantir_gitignore(tmp_path) is False
    texto = (tmp_path / ".gitignore").read_text()
    assert texto.startswith("node_modules\n") and texto.count(".firma/") == 1


def test_init_completo(tmp_path, monkeypatch):
    raiz = _repo(tmp_path / "app")
    (raiz / "pyproject.toml").write_text('[project]\nname="app"\n')
    (raiz / "tests").mkdir()
    monkeypatch.chdir(raiz)

    r = CliRunner().invoke(app, ["init", "--sim"])
    assert r.exit_code == 0, r.output
    assert "kaueboni/app" in r.output
    assert (raiz / ".firma" / "config.toml").exists()
    assert (raiz / ".firma" / "backlog").is_dir()
    assert (raiz / ".firma" / "INBOX.md").exists()
    assert ".firma/" in (raiz / ".gitignore").read_text()
    assert [p["nome"] for p in registro.listar()] == ["app"]

    r2 = CliRunner().invoke(app, ["init", "--sim"])
    assert r2.exit_code == 1 and "--forcar" in r2.output

    r3 = CliRunner().invoke(app, ["init", "--sim", "--forcar"])
    assert r3.exit_code == 0 and "já estava registrado" in r3.output
    assert len(registro.listar()) == 1


def test_init_cancelado_nao_cria_nada(tmp_path, monkeypatch):
    raiz = _repo(tmp_path / "app")
    monkeypatch.chdir(raiz)
    r = CliRunner().invoke(app, ["init"], input="n\n")
    assert r.exit_code == 1
    assert not (raiz / ".firma").exists()
