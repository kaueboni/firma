"""Firma v2: revisão dupla da spec, fallback Claude → Gemini, handoff, retomada, estado v2, skills."""

import json

import anyio
import pytest

from firma import config, executor, gitops, prd
from firma.estado import EstadoCorrompido, EstadoPrd
from firma.guarda import avaliar
from firma.runtime import erros
from firma.runtime.base import Resposta

from test_executor import Roteiro, _git, _rodar, _sem_fallback, projeto  # noqa: F401

INBOX = ".firma/INBOX.md"
BACKLOG = ".firma/backlog/PRD-001-carrinho.md"


# ---------------------------------------------------------------- spec


class SpecReprovada(Roteiro):
    def __init__(self, revisoes, **kw):
        super().__init__(**kw)
        self.revisoes = list(revisoes)

    async def __call__(self, pedido, quota):
        if pedido.papel == "advisor" and "Revise" in pedido.prompt:
            self.chamadas.append("advisor")
            self.pedidos.append(pedido)
            return Resposta(self.revisoes.pop(0), session_id="s-adv")
        return await super().__call__(pedido, quota)


def test_spec_reprovada_e_corrigida_passa_pela_segunda_revisao(projeto):
    roteiro = SpecReprovada(["falta X\n**VEREDITO:** REPROVADO", "ok\nVEREDITO: **APROVADO**"])
    _rodar(roteiro)
    assert roteiro.chamadas[:4] == ["tech-lead", "advisor", "tech-lead", "advisor"]
    assert "developer" in roteiro.chamadas
    assert "O advisor reprovou a spec" in roteiro.pedidos[2].prompt


def test_spec_reprovada_duas_vezes_estaciona(projeto):
    roteiro = SpecReprovada(["VEREDITO: REPROVADO", "ainda falta\nVEREDITO: REPROVADO"])
    resumo, _ = _rodar(roteiro)
    assert resumo["estacionado"] == ["app/PRD-001"]
    assert "developer" not in roteiro.chamadas
    inbox = (projeto / INBOX).read_text(encoding="utf-8")
    assert "mesmo após a correção do tech-lead" in inbox and "ainda falta" in inbox


def test_veredito_com_markdown():
    assert executor.veredito("**VEREDITO:** APROVADO")
    assert executor.veredito("VEREDITO: **APROVADO**")
    assert executor.veredito("**VEREDITO**: aprovado")
    assert not executor.veredito("**VEREDITO:** APROVADO\n\nVEREDITO: REPROVADO")


# ---------------------------------------------------------------- tarefas


def test_anotacao_do_advisor_nao_mascara_rodada_vazia(projeto):
    class DevPreguicoso(Roteiro):
        async def __call__(self, pedido, quota):
            if pedido.papel == "developer":
                self.chamadas.append("developer")
                self.pedidos.append(pedido)
                return Resposta("não fiz nada", session_id="s-dev")
            return await super().__call__(pedido, quota)

    roteiro = DevPreguicoso(advisor_diag="Crie o arquivo.")
    resumo, _ = _rodar(roteiro)
    assert resumo["estacionado"] == ["app/PRD-001"]
    assert "auditor" not in roteiro.chamadas and roteiro.chamadas.count("developer") == 4
    # o diagnóstico foi anotado em decisoes.md, mas a rodada final continua sem mudança de código
    wt = projeto / ".firma" / "worktrees" / "PRD-001"
    assert "Diagnóstico da T1" in (wt / "docs/firma/PRD-001/decisoes.md").read_text(encoding="utf-8")


def test_auditor_que_escreve_e_desfeito(projeto):
    class AuditorIntrometido(Roteiro):
        async def __call__(self, pedido, quota):
            if pedido.papel == "auditor":
                (pedido.cwd / "intruso.py").write_text("x\n", encoding="utf-8")
            return await super().__call__(pedido, quota)

    _rodar(AuditorIntrometido())
    wt = projeto / ".firma" / "worktrees" / "PRD-001"
    log = gitops.git(wt, "log", "--name-only", "--format=")
    assert "intruso.py" not in log and "mudanca_1.py" in log


def test_retomada_exata_da_rodada(projeto):
    class ParaNaSegunda(Roteiro):
        async def __call__(self, pedido, quota):
            r = await super().__call__(pedido, quota)
            if pedido.papel == "auditor" and self.chamadas.count("auditor") == 1:
                executor.pedir_parada()
            return r

    resumo, _ = _rodar(ParaNaSegunda(auditor=["falta teste\nVEREDITO: REPROVADO"]))
    assert "parada" in resumo["pausa"]
    estado = EstadoPrd.carregar(projeto, "PRD-001")
    assert estado.tarefa_ativa == "T1" and estado.rodada_atual == 2
    assert "falta teste" in estado.ultimo_retorno and len(estado.historico_rodadas) == 1

    roteiro = Roteiro()
    _rodar(roteiro)
    dev = next(p for p in roteiro.pedidos if p.papel == "developer")
    assert "falta teste" in dev.prompt  # retoma com o retorno da rodada anterior
    assert "Handoff da Firma" in dev.prompt  # e com o handoff (a worktree tinha mudança pendente)
    assert EstadoPrd.carregar(projeto, "PRD-001").feitas == ["T1", "T2"]


# ---------------------------------------------------------------- quota, fallback e erros


class SemQuotaNoClaude(Roteiro):
    """O Claude esgota no primeiro developer, depois de já ter mexido na worktree."""

    async def __call__(self, pedido, quota):
        if pedido.provider == "anthropic" and pedido.papel == "developer":
            self.chamadas.append("developer@anthropic")
            self.pedidos.append(pedido)
            (pedido.cwd / "parcial.py").write_text("meio caminho\n", encoding="utf-8")
            return Resposta("", erro="limite", tipo_erro=erros.QUOTA, sem_quota=True)
        return await super().__call__(pedido, quota)


def test_quota_do_claude_aciona_fallback_com_handoff(projeto):
    roteiro = SemQuotaNoClaude()
    resumo, eventos = _rodar(roteiro)
    assert resumo["estacionado"] == ["app/PRD-001"]  # sem GitHub: termina estacionado com tudo pronto
    google = [p for p in roteiro.pedidos if p.provider == "google"]
    assert google and google[0].papel == "developer"
    assert "Handoff da Firma" in google[0].prompt and "parcial.py" in google[0].prompt
    assert google[0].resume is None  # sessão nunca reaproveitada entre provedores
    # depois de esgotar, o resto do turno vai direto para o fallback
    depois = roteiro.pedidos[roteiro.pedidos.index(google[0]):]
    assert all(p.provider == "google" for p in depois)
    assert any("alternando para google/" in e for e in eventos)
    wt = projeto / ".firma" / "worktrees" / "PRD-001"
    assert "parcial.py" in gitops.git(wt, "log", "--name-only", "--format=")  # trabalho parcial preservado
    assert (projeto / ".firma" / "estado" / "PRD-001-handoff.md").exists()


def test_quota_sem_fallback_pausa(projeto):
    _sem_fallback(projeto)
    resumo, _ = _rodar(SemQuotaNoClaude())
    assert "quota" in resumo["pausa"]
    assert prd.ler(projeto / BACKLOG).status == "pronto"


def test_erro_de_autenticacao_estaciona_sem_fallback(projeto):
    class Expirado(Roteiro):
        async def __call__(self, pedido, quota):
            if pedido.papel == "developer":
                self.pedidos.append(pedido)
                return Resposta("", erro="401 token expirado", tipo_erro=erros.AUTENTICACAO)
            return await super().__call__(pedido, quota)

    roteiro = Expirado()
    resumo, _ = _rodar(roteiro)
    assert resumo["estacionado"] == ["app/PRD-001"]
    assert not any(p.provider == "google" for p in roteiro.pedidos)
    assert "autenticacao" in (projeto / INBOX).read_text(encoding="utf-8")


def test_teto_com_fallback_segue_no_gemini(projeto):
    class Cara(Roteiro):
        async def __call__(self, pedido, quota):
            r = await super().__call__(pedido, quota)
            if pedido.papel == "developer":
                quota.semanal = 0.65
            return r

    roteiro = Cara()
    resumo, _ = _rodar(roteiro)
    assert resumo["pausa"] == ""
    assert roteiro.pedidos[-1].provider == "google"


def test_classificacao_de_erros():
    assert erros.classificar(429, "rate limited, retry") == erros.TRANSITORIO
    assert erros.classificar(429, "You have reached your usage limit") == erros.QUOTA
    assert erros.classificar(529, "overloaded") == erros.TRANSITORIO
    assert erros.classificar(401, "") == erros.AUTENTICACAO
    assert erros.classificar(404, "model not found") == erros.CONFIGURACAO
    assert erros.classificar(None, "getaddrinfo ENOTFOUND api") == erros.REDE
    assert erros.classificar(None, "RESOURCE_EXHAUSTED: quota exceeded") == erros.QUOTA
    assert erros.fatal(erros.REDE) and not erros.fatal(erros.QUOTA)


# ---------------------------------------------------------------- estado


def test_estado_v1_migra(tmp_path):
    c = EstadoPrd.caminho(tmp_path, "PRD-001")
    c.parent.mkdir(parents=True)
    c.write_text(json.dumps({"fase": "tarefas", "feitas": ["T1"], "sessoes": {"developer": "abc"}, "pr": "", "decisoes": []}))
    e = EstadoPrd.carregar(tmp_path, "PRD-001")
    assert e.schema_version == 2 and e.feitas == ["T1"] and e.rodada_atual == 1
    assert e.sessao("developer", "anthropic", "sonnet") == "abc"  # sessão v1 vale para o Claude
    assert e.sessao("developer", "google", "gemini-2.5-pro") is None
    e.salvar(tmp_path, "PRD-001")
    assert json.loads(c.read_text())["schema_version"] == 2
    assert not c.with_name(c.name + ".tmp").exists()


def test_estado_corrompido_nao_vira_vazio(tmp_path):
    c = EstadoPrd.caminho(tmp_path, "PRD-001")
    c.parent.mkdir(parents=True)
    c.write_text('{"fase": "tarefas", "feitas": ["T1"')
    with pytest.raises(EstadoCorrompido):
        EstadoPrd.carregar(tmp_path, "PRD-001")


def test_estado_corrompido_estaciona(projeto):
    c = EstadoPrd.caminho(projeto, "PRD-001")
    c.parent.mkdir(parents=True, exist_ok=True)
    c.write_text("{quebrado")
    resumo, _ = _rodar(Roteiro())
    assert resumo["estacionado"] == ["app/PRD-001"]
    assert c.read_text() == "{quebrado"  # não sobrescreve


# ---------------------------------------------------------------- config


def _cfg(tmp_path, texto):
    (tmp_path / ".firma").mkdir()
    (tmp_path / ".firma" / "config.toml").write_text(texto, encoding="utf-8")
    return config.carregar(tmp_path)


def test_config_legada(tmp_path):
    cfg = _cfg(tmp_path, '[modelos]\ntrabalho = "sonnet"\nadvisor = "opus"\n')
    dev = config.modelo_do_papel(cfg, "developer")
    assert (dev.provider, dev.model, dev.fallback_provider) == ("anthropic", "sonnet", "google")
    assert config.modelo_do_papel(cfg, "advisor").model == "opus"


def test_config_por_papel(tmp_path):
    cfg = _cfg(tmp_path, '[modelos.developer]\nprovider = "google"\nmodel = "gemini-2.5-pro"\n')
    dev = config.modelo_do_papel(cfg, "developer")
    assert (dev.provider, dev.model, dev.tem_fallback) == ("google", "gemini-2.5-pro", False)
    assert config.modelo_do_papel(cfg, "tech-lead").provider == "anthropic"
    assert config.precisa_opencode(cfg)


def test_init_gera_config_por_papel(projeto):
    cfg = config.carregar(projeto)
    for papel in ("tech-lead", "advisor", "developer", "auditor"):
        m = config.modelo_do_papel(cfg, papel)
        assert m.provider == "anthropic" and m.fallback_provider == "google"


# ---------------------------------------------------------------- guarda


def test_guarda_v2(tmp_path):
    docs = "docs/firma/PRD-001"
    assert avaliar("Write", {"file_path": str(tmp_path / ".claude/skills/x/SKILL.md")}, tmp_path) is not None
    assert avaliar("Edit", {"file_path": str(tmp_path / ".opencode/x")}, tmp_path) is not None
    assert avaliar("Write", {"file_path": str(tmp_path / ".git/config")}, tmp_path) is not None
    assert avaliar("Write", {"file_path": str(tmp_path / docs / "plano.md")}, tmp_path, escrita_proibida=[docs]) is not None
    assert avaliar("Write", {"file_path": str(tmp_path / "app.py")}, tmp_path, escrita_proibida=[docs]) is None
    assert avaliar("Read", {"file_path": "C:/Users/x/.local/share/opencode/auth.json"}, tmp_path) is not None
    assert avaliar("Bash", {"command": "cat ~/.local/share/opencode/auth.json"}, tmp_path) is not None
    assert avaliar("Read", {"file_path": str(tmp_path / ".claude/skills/x/SKILL.md")}, tmp_path) is None
    assert avaliar("shell", {"command": "git push"}, tmp_path) is not None  # nome da ferramenta no OpenCode


# ---------------------------------------------------------------- skills e ambientes na worktree


def test_skills_e_instrucoes_ligadas_e_fora_do_commit(projeto):
    (projeto / ".claude" / "skills" / "stack").mkdir(parents=True)
    (projeto / ".claude" / "skills" / "stack" / "SKILL.md").write_text("---\nname: stack\n---\n")
    (projeto / ".opencode" / "skills" / "op").mkdir(parents=True)
    (projeto / ".opencode" / "skills" / "op" / "SKILL.md").write_text("x")
    (projeto / "AGENTS.md").write_text("regras\n")
    wt = gitops.preparar_worktree(projeto, "PRD-010", "main")
    ligados = gitops.ligar_ambientes(projeto, wt, [])
    assert {".claude", ".opencode", "AGENTS.md"} <= set(ligados)
    assert (wt / ".claude" / "skills" / "stack" / "SKILL.md").exists()
    assert not gitops.tem_mudancas(wt)
    assert gitops.commit(wt, "nada") is None


def test_claude_commitado_liga_so_as_skills(projeto):
    (projeto / ".claude").mkdir()
    (projeto / ".claude" / "settings.json").write_text("{}")
    _git(projeto, "add", "-f", ".claude/settings.json")
    _git(projeto, "commit", "-qm", "settings")
    (projeto / ".claude" / "skills" / "s").mkdir(parents=True)
    (projeto / ".claude" / "skills" / "s" / "SKILL.md").write_text("x")
    wt = gitops.preparar_worktree(projeto, "PRD-011", "main")
    ligados = gitops.ligar_ambientes(projeto, wt, [])
    assert ".claude/skills" in ligados and ".claude" not in ligados
    assert (wt / ".claude" / "skills" / "s" / "SKILL.md").exists()
    assert not gitops.tem_mudancas(wt)


def test_foto_e_restaurar(projeto):
    wt = gitops.preparar_worktree(projeto, "PRD-012", "main")
    (wt / "dev.py").write_text("do developer\n")
    retrato = gitops.foto(wt)
    (wt / "dev.py").write_text("mexido pelo auditor\n")
    (wt / "novo.py").write_text("x")
    (wt / "README.md").write_text("estragado")
    desfeitos = gitops.restaurar(wt, retrato)
    assert sorted(desfeitos) == ["README.md", "dev.py", "novo.py"]
    assert (wt / "dev.py").read_text() == "do developer\n" and not (wt / "novo.py").exists()
    assert (wt / "README.md").read_text() == "app\n"


def test_renovacao_da_trava(projeto, monkeypatch):
    from firma import trava

    trava.criar_trava()
    import os, time

    antigo = time.time() - 3600
    os.utime(trava.arquivo_trava(), (antigo, antigo))
    trava.renovar_trava()  # dentro do intervalo de 15s: não toca
    assert trava.trava_ativa() is None
    trava.renovar_trava(forcar=True)
    assert trava.trava_ativa() is not None
    trava.soltar_trava()
