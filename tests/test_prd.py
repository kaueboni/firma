import subprocess
from datetime import date

import pytest
from typer.testing import CliRunner

from firma import claude, prd
from firma.cli import app

PRONTO = """---
id: PRD-001
tipo: feature
status: rascunho
prioridade: 2
tamanho: M
depende_de: []
---
# Carrinho compartilhado

## Problema
Hoje o pedido só aceita um produto.

## Comportamento esperado
Cliente e agente adicionam e removem itens antes de confirmar.

## Critérios de aceite
- [ ] O teste `test_carrinho_dois_itens` passa
- [ ] O evento STATE_DELTA chega ao front em até 1 run

## Fora de escopo
- Persistir o carrinho entre sessões

## Limites do advisor
- Não muda o contrato de confirmar_pedido sem perguntar

## Perguntas em aberto
- nenhuma
"""


def _escrever(pasta, texto, nome="PRD-001-carrinho.md"):
    caminho = pasta / nome
    caminho.write_text(texto, encoding="utf-8")
    return caminho


def test_prd_completo_passa(tmp_path):
    r = prd.checar(prd.ler(_escrever(tmp_path, PRONTO)))
    assert r.pronto, r.erros
    assert r.avisos == []


def test_modelo_novo_nao_passa(tmp_path):
    caminho = prd.criar(tmp_path, "Carrinho", "feature")
    r = prd.checar(prd.ler(caminho))
    assert not r.pronto
    assert any("[PREENCHER]" in e for e in r.erros)
    assert any("pergunta" in e for e in r.erros)


@pytest.mark.parametrize(
    "tipo,tem,nao_tem",
    [
        ("feature", "## Comportamento esperado", "## Como reproduzir"),
        ("bug", "## Teste de regressão", "## Comportamento esperado"),
        ("refatoracao", "## O que não pode mudar", "## Atual vs. esperado"),
    ],
)
def test_modelo_so_traz_secoes_do_tipo(tipo, tem, nao_tem):
    texto = prd.renderizar("X", tipo, "PRD-007", date(2026, 11, 12))
    assert tem in texto and nao_tem not in texto
    assert "<!--" not in texto
    assert "id: PRD-007" in texto and "criado_em: 2026-11-12" in texto


def test_tamanho_g_pede_para_quebrar(tmp_path):
    r = prd.checar(prd.ler(_escrever(tmp_path, PRONTO.replace("tamanho: M", "tamanho: G"))))
    assert any("quebre" in e for e in r.erros)


def test_pergunta_em_aberto_bloqueia(tmp_path):
    r = prd.checar(prd.ler(_escrever(tmp_path, PRONTO.replace("- nenhuma", "- O carrinho tem limite?"))))
    assert any("pergunta" in e for e in r.erros)


def test_secao_do_tipo_faltando(tmp_path):
    r = prd.checar(prd.ler(_escrever(tmp_path, PRONTO.replace("tipo: feature", "tipo: bug"))))
    assert any("Como reproduzir" in e for e in r.erros)


def test_criterio_vago_gera_aviso(tmp_path):
    texto = PRONTO.replace("- [ ] O teste `test_carrinho_dois_itens` passa", "- [ ] A experiência fica melhor")
    r = prd.checar(prd.ler(_escrever(tmp_path, texto)))
    assert r.pronto
    assert any("vago" in a for a in r.avisos)


def test_dependencia_inexistente_avisa(tmp_path):
    texto = PRONTO.replace("depende_de: []", "depende_de: [PRD-009]")
    r = prd.checar(prd.ler(_escrever(tmp_path, texto)), {"PRD-001"})
    assert any("PRD-009" in a for a in r.avisos)


def test_sem_cabecalho_da_erro(tmp_path):
    with pytest.raises(ValueError):
        prd.ler(_escrever(tmp_path, "# só título\n"))


def test_aceita_bom_do_bloco_de_notas(tmp_path):
    caminho = tmp_path / "PRD-001-x.md"
    caminho.write_bytes(PRONTO.encode("utf-8-sig"))
    assert prd.checar(prd.ler(caminho)).pronto


def test_ids_e_busca(tmp_path):
    assert prd.proximo_id(tmp_path) == "PRD-001"
    _escrever(tmp_path, PRONTO)
    assert prd.proximo_id(tmp_path) == "PRD-002"
    assert prd.encontrar(tmp_path, "PRD-001").name == "PRD-001-carrinho.md"
    assert prd.encontrar(tmp_path, "1").name == "PRD-001-carrinho.md"
    assert prd.encontrar(tmp_path, "PRD-002") is None


def test_slug():
    assert prd.slug("Carrinho como estado compartilhado (AG-UI)") == "carrinho-como-estado-compartilhado-ag-ui"


def test_mudar_status_preserva_o_resto(tmp_path):
    caminho = _escrever(tmp_path, PRONTO)
    prd.mudar_status(caminho, "pronto")
    novo = prd.ler(caminho)
    assert novo.status == "pronto"
    assert novo.secoes == prd.ler(_escrever(tmp_path, PRONTO, "PRD-002-x.md")).secoes


# ---------------------------------------------------------------- CLI


@pytest.fixture
def projeto_firma(tmp_path, monkeypatch):
    monkeypatch.setenv("FIRMA_HOME", str(tmp_path / "home"))
    raiz = tmp_path / "app"
    subprocess.run(["git", "init", "-q", "-b", "main", str(raiz)], check=True)
    monkeypatch.chdir(raiz)
    assert CliRunner().invoke(app, ["init", "--sim"]).exit_code == 0
    return raiz


def test_fluxo_do_cli(projeto_firma):
    runner = CliRunner()
    r = runner.invoke(app, ["prd", "novo", "Carrinho compartilhado", "--tipo", "feature"])
    assert r.exit_code == 0, r.output
    caminho = projeto_firma / ".firma" / "backlog" / "PRD-001-carrinho-compartilhado.md"
    assert caminho.exists()

    assert runner.invoke(app, ["prd", "checar", "1"]).exit_code == 1
    r = runner.invoke(app, ["prd", "pronto", "PRD-001"])
    assert r.exit_code == 1 and "continua em rascunho" in r.output

    caminho.write_text(PRONTO, encoding="utf-8")
    assert runner.invoke(app, ["prd", "checar", "PRD-001"]).exit_code == 0
    r = runner.invoke(app, ["prd", "pronto", "PRD-001"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["prd", "listar"])
    assert "PRD-001" in r.output and "pronto" in r.output


def test_prd_sem_init(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(app, ["prd", "listar"])
    assert r.exit_code == 1 and "firma init" in r.output


def test_claude_instalar(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert claude.instalar() == [("prd", "instalado")]
    assert claude.instalar() == [("prd", "igual")]
    (tmp_path / "commands" / "prd.md").write_text("meu", encoding="utf-8")
    assert claude.instalar() == [("prd", "editado")]
    assert claude.instalar(forcar=True) == [("prd", "atualizado")]
    texto = (tmp_path / "commands" / "prd.md").read_text(encoding="utf-8")
    assert texto.startswith("---\ndescription:") and "$ARGUMENTS" in texto
