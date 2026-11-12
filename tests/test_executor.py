import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from typer.testing import CliRunner

from firma import executor, prd, registro
from firma.cli import app
from firma.sessao import Resposta

from test_prd import PRONTO

TESTE_OK = f'"{sys.executable}" -c "import sys; sys.exit(0)"'
TESTE_FALHA = f'"{sys.executable}" -c "import sys; sys.exit(1)"'


def _git(pasta, *args):
    subprocess.run(["git", "-C", str(pasta), *args], check=True, capture_output=True)


@pytest.fixture
def projeto(tmp_path, monkeypatch):
    monkeypatch.setenv("FIRMA_HOME", str(tmp_path / "home"))
    raiz = tmp_path / "app"
    raiz.mkdir()
    _git(raiz, "init", "-q", "-b", "main")
    _git(raiz, "config", "user.email", "t@t")
    _git(raiz, "config", "user.name", "t")
    (raiz / "README.md").write_text("app\n")
    _git(raiz, "add", ".")
    _git(raiz, "commit", "-q", "-m", "inicio")
    monkeypatch.chdir(raiz)
    assert CliRunner().invoke(app, ["init", "--sim"]).exit_code == 0
    _git(raiz, "add", ".gitignore")
    _git(raiz, "commit", "-qm", "gitignore")
    cfg = raiz / ".firma" / "config.toml"
    texto = cfg.read_text(encoding="utf-8")
    texto += f'\n[[partes]]\npasta = "."\ntestes = {TESTE_OK!r}\nlint = ""\n'.replace("'", "'")
    cfg.write_text(texto.replace('[[partes]]\npasta = "."\ntestes = ""\nlint = ""\n', ""), encoding="utf-8")
    caminho = raiz / ".firma" / "backlog" / "PRD-001-carrinho.md"
    caminho.write_text(PRONTO.replace("status: rascunho", "status: pronto"), encoding="utf-8")
    return raiz


class Roteiro:
    """Sessões falsas: cada papel faz o mínimo para o turno andar, com respostas configuráveis."""

    def __init__(self, auditor=None, advisor_diag="Corrija o nome do arquivo."):
        self.auditor = list(auditor or ["VEREDITO: APROVADO"])
        self.advisor_diag = advisor_diag
        self.chamadas = []

    async def __call__(self, pedido, quota):
        self.chamadas.append(pedido.papel)
        docs = pedido.cwd / "docs" / "firma" / "PRD-001"
        if pedido.papel == "tech-lead":
            (docs / "spec.md").write_text("spec\n", encoding="utf-8")
            (docs / "plano.md").write_text("- [ ] T1: estado do carrinho\n  teste\n- [ ] T2: remover item\n  teste\n", encoding="utf-8")
            return Resposta("feito", session_id="s-tl")
        if pedido.papel == "advisor":
            if "Revise" in pedido.prompt:
                return Resposta("ok\nVEREDITO: APROVADO", session_id="s-adv")
            return Resposta(self.advisor_diag, session_id="s-adv")
        if pedido.papel == "developer":
            n = len([c for c in self.chamadas if c == "developer"])
            (pedido.cwd / f"mudanca_{n}.py").write_text("x = 1\n", encoding="utf-8")
            return Resposta("implementei", session_id="s-dev")
        if pedido.papel == "auditor":
            texto = self.auditor.pop(0) if len(self.auditor) > 1 else self.auditor[0]
            return Resposta(texto, session_id="s-aud")
        raise AssertionError(pedido.papel)


def _rodar(roteiro, **kw):
    eventos = []
    resumo = anyio.run(lambda: executor.rodar_turno(roteiro, lambda t, **d: eventos.append(t), sem_pr=True, **kw))
    return resumo, eventos


def test_turno_completo_sem_github(projeto):
    roteiro = Roteiro()
    resumo, eventos = _rodar(roteiro)
    assert roteiro.chamadas == ["tech-lead", "advisor", "developer", "auditor", "developer", "auditor"]
    wt = projeto / ".firma" / "worktrees" / "PRD-001"
    log = subprocess.run(["git", "-C", str(wt), "log", "--format=%s"], capture_output=True, text=True).stdout
    assert "feat(PRD-001): T2 — remover item" in log and "docs(PRD-001): spec e plano" in log
    assert "- [x] T1" in (wt / "docs/firma/PRD-001/plano.md").read_text()
    # sem GitHub configurado não há PR: fica estacionado com o aviso no INBOX
    assert resumo["estacionado"] == ["app/PRD-001"]
    assert "sem GitHub" in (projeto / ".firma" / "INBOX.md").read_text()
    assert executor.trava_ativa() is None


def test_reprovacao_volta_para_o_developer(projeto):
    roteiro = Roteiro(auditor=["falta teste\nVEREDITO: REPROVADO", "VEREDITO: APROVADO"])
    _rodar(roteiro)
    assert roteiro.chamadas[:6] == ["tech-lead", "advisor", "developer", "auditor", "developer", "auditor"]


def test_bloqueio_do_advisor_estaciona(projeto):
    roteiro = Roteiro(auditor=["VEREDITO: REPROVADO"], advisor_diag="BLOQUEIO: precisa de dependência nova")
    resumo, _ = _rodar(roteiro)
    assert resumo["estacionado"] == ["app/PRD-001"]
    assert roteiro.chamadas.count("auditor") == 3
    assert "BLOQUEIO" in (projeto / ".firma" / "INBOX.md").read_text()
    assert prd.ler(projeto / ".firma/backlog/PRD-001-carrinho.md").status == "estacionado"


def test_teto_da_quota_pausa_e_retoma(projeto):
    class Cara(Roteiro):
        async def __call__(self, pedido, quota):
            r = await super().__call__(pedido, quota)
            if pedido.papel == "developer":
                quota.semanal = 0.65  # passou do teto de 60%
            return r

    resumo, _ = _rodar(Cara())
    assert "quota semanal" in resumo["pausa"]
    caminho = projeto / ".firma/backlog/PRD-001-carrinho.md"
    assert prd.ler(caminho).status == "pronto"  # volta para a fila
    estado = executor.EstadoPrd.carregar(projeto, "PRD-001")
    assert estado.fase == "tarefas" and estado.feitas == []

    roteiro = Roteiro()
    resumo, _ = _rodar(roteiro)  # retoma: não refaz a spec
    assert roteiro.chamadas[0] == "developer"
    assert executor.EstadoPrd.carregar(projeto, "PRD-001").feitas == ["T1", "T2"]


def test_verificacao_falhando_nao_chama_auditor(projeto):
    cfg = projeto / ".firma" / "config.toml"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace(TESTE_OK, TESTE_FALHA), encoding="utf-8")
    roteiro = Roteiro(advisor_diag="BLOQUEIO: teste quebrado")
    resumo, _ = _rodar(roteiro)
    assert "auditor" not in roteiro.chamadas
    assert resumo["estacionado"]


def test_parada_pedida(projeto):
    executor.pedir_parada()
    # rodar_turno limpa um pedido antigo ao começar; simulamos o pedido durante o turno
    class ParaNoDev(Roteiro):
        async def __call__(self, pedido, quota):
            r = await super().__call__(pedido, quota)
            if pedido.papel == "advisor":
                executor.pedir_parada()
            return r

    resumo, _ = _rodar(ParaNoDev())
    assert "parada pedida" in resumo["pausa"]


def test_max_e_fila_vazia(projeto):
    resumo, eventos = _rodar(Roteiro(), max_prds=0)
    assert resumo["pr-aberto"] == [] and resumo["estacionado"] == []
    (projeto / ".firma/backlog/PRD-001-carrinho.md").unlink()
    _, eventos = _rodar(Roteiro())
    assert any("nada na fila" in e for e in eventos)


def test_tarefas_e_veredito():
    plano = "- [ ] T1: a\n  x\n- [x] T2. b\n* nada\n- [ ] T3 — c"
    assert executor.tarefas(plano) == [("T1", "a", False), ("T2", "b", True), ("T3", "c", False)]
    assert executor.veredito("bla\nVEREDITO: APROVADO")
    assert not executor.veredito("VEREDITO: APROVADO\n...\nVEREDITO: REPROVADO")
    assert not executor.veredito("sem veredito")


def test_janela():
    from datetime import datetime

    assert executor.dentro_da_janela("", datetime(2026, 1, 1, 12))
    assert executor.dentro_da_janela("23:00-07:00", datetime(2026, 1, 1, 2))
    assert not executor.dentro_da_janela("23:00-07:00", datetime(2026, 1, 1, 12))
    assert executor.dentro_da_janela("09:00-18:00", datetime(2026, 1, 1, 12))


def test_worktree_usa_os_ambientes_do_repositorio(projeto):
    from firma import gitops

    (projeto / "agent").mkdir()
    (projeto / "agent" / "app.py").write_text("x\n")
    _git(projeto, "add", "agent")
    _git(projeto, "commit", "-qm", "agent")
    (projeto / "agent" / ".venv" / "bin").mkdir(parents=True)
    (projeto / "node_modules").mkdir()
    wt = gitops.preparar_worktree(projeto, "PRD-009", "main")
    ligados = gitops.ligar_ambientes(projeto, wt, [{"pasta": "agent"}])
    assert sorted(ligados) == ["agent/.venv", "node_modules"]
    assert (wt / "agent" / ".venv" / "bin").is_dir()
    # mesmo sem .gitignore para eles, os ambientes ligados não contam como mudança nem entram em commit
    assert not gitops.tem_mudancas(wt)
    assert gitops.commit(wt, "nada") is None


def test_erro_inesperado_estaciona_e_conta(projeto):
    class Quebra(Roteiro):
        async def __call__(self, pedido, quota):
            if pedido.papel == "developer":
                raise ValueError("falha estranha do SDK")
            return await super().__call__(pedido, quota)

    resumo, eventos = _rodar(Quebra())
    assert resumo["estacionado"] == ["app/PRD-001"]
    assert any("ValueError" in e for e in eventos)
    assert "ValueError" in (projeto / ".firma" / "INBOX.md").read_text()
    assert executor.trava_ativa() is None


def test_caminho_curto_e_feed():
    from firma.sessao import _Feed, caminho_curto

    wt = Path("C:/projetos/poc/.firma/worktrees/PRD-001")
    assert caminho_curto("C:\\projetos\\poc\\.firma\\worktrees\\PRD-001\\agent\\app.py", wt) == "agent/app.py"
    assert caminho_curto("C:\\projetos\\poc\\agent\\.venv\\Lib\\site-packages\\ag_ui_strands\\agent.py", wt) == "[lib] ag_ui_strands/agent.py"
    assert caminho_curto("C:\\projetos\\poc\\README.md", wt) == "README.md"
    assert caminho_curto("C:\\projetos\\poc", wt) == "."
    linhas = []
    feed = _Feed(lambda t, **k: linhas.append(t), "PRD-001", "dev")
    for t in ["Read a", "Read a", "Read a", "Read b"]:
        feed(t)
    feed.fechar()
    assert linhas == ["Read a", "  ↳ mais 2 vez(es)", "Read b"]
