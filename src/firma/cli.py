"""Linha de comando da Firma."""

from __future__ import annotations

import anyio
import typer

from firma import __version__
from firma.doctor import AVISO, FALHA, OK, rodar_checagens

app = typer.Typer(help="Firma: PRDs entram, PRs saem.", no_args_is_help=True, add_completion=False)

_SIMBOLO = {OK: "✓", AVISO: "!", FALHA: "✗"}


def _versao(valor: bool) -> None:
    if valor:
        typer.echo(f"firma {__version__}")
        raise typer.Exit()


@app.callback()
def principal(
    versao: bool = typer.Option(False, "--version", callback=_versao, is_eager=True, help="Mostra a versão."),
) -> None:
    """Firma: um time de agentes que transforma PRDs em PRs."""


@app.command()
def doctor(
    quota: bool = typer.Option(
        False, "--quota", help="Faz uma chamada mínima pelo Agent SDK e mostra o estado da quota."
    ),
) -> None:
    """Confere se esta máquina tem o que a Firma precisa."""
    checagens = rodar_checagens()
    for c in checagens:
        typer.echo(f" {_SIMBOLO[c.estado]} {c.nome}: {c.detalhe}")

    falhou = any(c.estado == FALHA for c in checagens)
    if quota:
        if falhou:
            typer.echo("\nCorrija as falhas acima antes de sondar a quota.")
            raise typer.Exit(1)
        _sondar_quota()
    if falhou:
        raise typer.Exit(1)


@app.command()
def init(
    sim: bool = typer.Option(False, "--sim", "-y", help="Não pede confirmação."),
    forcar: bool = typer.Option(False, "--forcar", help="Reescreve um config.toml que já existe."),
) -> None:
    """Prepara o projeto atual para a Firma: detecta, mostra e cria a pasta .firma/."""
    from pathlib import Path

    from firma import projeto, registro

    d = projeto.detectar(Path.cwd())
    if d is None:
        typer.echo("Esta pasta não está dentro de um repositório git.")
        raise typer.Exit(1)

    config = d.raiz / projeto.PASTA / "config.toml"
    if config.exists() and not forcar:
        typer.echo(f"A Firma já está configurada aqui ({config}). Use --forcar para reescrever.")
        raise typer.Exit(1)

    typer.echo(f"Projeto: {d.nome}  ({d.raiz})")
    typer.echo(f"  branch principal: {d.branch_principal}")
    typer.echo(f"  GitHub:           {d.github or '—'}")
    typer.echo(f"  CLAUDE.md:        {'sim' if d.tem_claude_md else 'não'}")
    if not d.partes:
        typer.echo("  partes:           nenhuma reconhecida")
    for p in d.partes:
        rotulo = "raiz" if p.pasta == "." else f"{p.pasta}/"
        typer.echo(f"  [{rotulo}] testes: {p.testes or '—'}   lint: {p.lint or '—'}")
    for aviso in d.avisos:
        typer.echo(f" ! {aviso}")
    typer.echo("\nVai criar .firma/ (config.toml, backlog/, INBOX.md), pôr .firma/ no .gitignore e registrar o projeto.")
    typer.echo("Nada no seu código, no CLAUDE.md ou em .claude/ é alterado.")

    if not sim and not typer.confirm("Continuar?", default=True):
        raise typer.Exit(1)

    base = projeto.criar_pasta(d)
    mudou_gitignore = projeto.garantir_gitignore(d.raiz)
    novo = registro.registrar(d.raiz, d.nome)
    typer.echo(f"\n ✓ {base} criada")
    if mudou_gitignore:
        typer.echo(" ✓ .firma/ acrescentada ao .gitignore (lembre de commitar essa linha)")
    typer.echo(" ✓ projeto registrado" if novo else " ✓ projeto já estava registrado")
    typer.echo(f"\nRevise {base / 'config.toml'} se algum comando estiver errado.")
    typer.echo("Próximo passo: `firma claude instalar` (uma vez por máquina) e `/prd <ideia>` no Claude Code.")


@app.command()
def projetos() -> None:
    """Lista os projetos registrados nesta máquina."""
    from firma import registro

    lista = registro.listar()
    if not lista:
        typer.echo("Nenhum projeto registrado. Rode `firma init` dentro de um repositório.")
        return
    for p in lista:
        typer.echo(f" • {p['nome']}  {p['caminho']}")


def _sondar_quota() -> None:
    from firma.quota import resumir, salvar, sondar

    typer.echo("\nSondando a quota (uma chamada mínima pelo Agent SDK)...")
    sonda = anyio.run(sondar)
    linhas, veredito = resumir(sonda)
    for linha in linhas:
        typer.echo(f"  {linha}")
    typer.echo("")
    for linha in veredito:
        typer.echo(f" → {linha}")
    typer.echo(f"\nResposta completa salva em {salvar(sonda)}")


# ---------------------------------------------------------------- firma prd

prd_app = typer.Typer(help="PRDs: criar, conferir e pôr na fila.", no_args_is_help=True)
app.add_typer(prd_app, name="prd")


def _backlog():
    from pathlib import Path

    from firma import projeto

    raiz = projeto.raiz_git(Path.cwd())
    base = raiz / projeto.PASTA if raiz else None
    if base is None or not (base / "config.toml").exists():
        typer.echo("A Firma não está configurada neste projeto. Rode `firma init` na raiz do repositório.")
        raise typer.Exit(1)
    return base / "backlog"


def _abrir(alvo: str):
    from firma import prd

    backlog = _backlog()
    caminho = prd.encontrar(backlog, alvo)
    if caminho is None:
        typer.echo(f"PRD não encontrado: {alvo}")
        raise typer.Exit(1)
    try:
        return backlog, caminho, prd.ler(caminho)
    except ValueError as e:
        typer.echo(f"{caminho.name}: {e}")
        raise typer.Exit(1)


def _mostrar_resultado(r) -> None:
    for e in r.erros:
        typer.echo(f" ✗ {e}")
    for a in r.avisos:
        typer.echo(f" ! {a}")


@prd_app.command("novo")
def prd_novo(
    titulo: str = typer.Argument(..., help="Título curto do PRD."),
    tipo: str = typer.Option("feature", "--tipo", "-t", help="feature, bug ou refatoracao."),
) -> None:
    """Cria um PRD no backlog a partir do modelo do tipo escolhido."""
    from firma import prd

    if tipo not in prd.TIPOS:
        typer.echo(f"Tipo inválido: {tipo}. Use {', '.join(prd.TIPOS)}.")
        raise typer.Exit(1)
    caminho = prd.criar(_backlog(), titulo, tipo)
    typer.echo(f"Criado: {caminho}")
    typer.echo("Preencha as seções com [PREENCHER] (ou use /prd no Claude Code) e rode `firma prd checar`.")


@prd_app.command("listar")
def prd_listar() -> None:
    """Lista os PRDs do backlog com tipo, status e tamanho."""
    from firma import prd

    prds = prd.listar(_backlog())
    if not prds:
        typer.echo("Backlog vazio. Crie um PRD com `firma prd novo` ou `/prd` no Claude Code.")
        return
    for p in prds:
        typer.echo(f" {p.id:8} {p.status:12} {p.tipo:12} {str(p.meta.get('tamanho', '?')):2} {p.titulo}")


@prd_app.command("checar")
def prd_checar(alvo: str = typer.Argument(..., help="Id (PRD-001 ou 1) ou caminho do arquivo.")) -> None:
    """Confere a regra de pronto sem mudar nada."""
    from firma import prd

    backlog, caminho, p = _abrir(alvo)
    r = prd.checar(p, {x.id for x in prd.listar(backlog)})
    _mostrar_resultado(r)
    if r.pronto:
        typer.echo(f" ✓ {p.id} passa na regra de pronto. Para pôr na fila: firma prd pronto {p.id}")
    else:
        raise typer.Exit(1)


@prd_app.command("pronto")
def prd_pronto(alvo: str = typer.Argument(..., help="Id (PRD-001 ou 1) ou caminho do arquivo.")) -> None:
    """Confere a regra de pronto e, se passar, muda o status para `pronto` (entra na fila)."""
    from firma import prd

    backlog, caminho, p = _abrir(alvo)
    r = prd.checar(p, {x.id for x in prd.listar(backlog)})
    _mostrar_resultado(r)
    if not r.pronto:
        typer.echo(f"\n{p.id} continua em {p.status}: corrija os erros acima.")
        raise typer.Exit(1)
    if p.status not in ("rascunho", "pronto"):
        typer.echo(f"{p.id} está em {p.status}; só PRDs em rascunho entram na fila.")
        raise typer.Exit(1)
    prd.mudar_status(caminho, "pronto")
    typer.echo(f" ✓ {p.id} está pronto e entra na fila.")


# ---------------------------------------------------------------- firma claude

claude_app = typer.Typer(help="Integração com o Claude Code.", no_args_is_help=True)
app.add_typer(claude_app, name="claude")


@claude_app.command("instalar")
def claude_instalar(
    forcar: bool = typer.Option(False, "--forcar", help="Sobrescreve comandos que você editou."),
) -> None:
    """Instala os comandos da Firma (/prd) no Claude Code, para todos os projetos."""
    from firma import claude

    for nome, situacao in claude.instalar(forcar):
        if situacao == "editado":
            typer.echo(f" ! /{nome}: você editou este comando; use --forcar para substituir")
        else:
            typer.echo(f" ✓ /{nome}: {situacao}")
    typer.echo(f"Pasta: {claude.pasta_comandos()}")
