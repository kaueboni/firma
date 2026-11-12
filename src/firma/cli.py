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
