import subprocess

from firma.doctor import FALHA, OK, checar_comando, checar_credencial, checar_python


def _resultado(code: int, out: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr="")


def test_python_antigo_falha():
    assert checar_python((3, 9)).estado == FALHA
    assert checar_python((3, 12)).estado == OK


def test_chave_de_api_no_ambiente_falha():
    c = checar_credencial({"ANTHROPIC_API_KEY": "sk-ant-x"})
    assert c.estado == FALHA
    assert "token" in c.detalhe


def test_token_da_assinatura_ok():
    assert checar_credencial({"CLAUDE_CODE_OAUTH_TOKEN": "x"}).estado == OK
    assert checar_credencial({}).estado == OK


def test_comando_ausente_falha():
    c = checar_comando("x", ["comando-que-nao-existe-123"], "instale")
    assert c.estado == FALHA


def test_comando_com_erro_mostra_dica():
    c = checar_comando("git", ["git", "--version"], "Instale o git.", executar=lambda cmd: _resultado(1))
    assert c.estado == FALHA
    assert c.detalhe == "Instale o git."


def test_comando_ok_usa_primeira_linha():
    c = checar_comando("git", ["git", "--version"], "", executar=lambda cmd: _resultado(0, "git version 2.45\n"))
    assert c.estado == OK
    assert c.detalhe == "git version 2.45"


def test_checar_opencode(monkeypatch):
    import subprocess as sp

    from firma import doctor
    from firma.runtime import opencode

    monkeypatch.setattr(opencode, "comando_opencode", lambda: None)
    assert doctor.checar_opencode().estado == doctor.FALHA
    monkeypatch.setattr(opencode, "comando_opencode", lambda: ["opencode"])
    velho = lambda cmd: sp.CompletedProcess(cmd, 0, "1.2.0\n", "")
    novo = lambda cmd: sp.CompletedProcess(cmd, 0, "1.19.5\n", "")
    assert doctor.checar_opencode(velho).estado == doctor.FALHA
    assert doctor.checar_opencode(novo).estado == doctor.OK
