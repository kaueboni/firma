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
