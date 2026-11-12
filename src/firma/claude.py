"""Comandos da Firma para o Claude Code, instalados no nível do usuário."""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path

COMANDOS = ("prd",)


def pasta_comandos() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "commands"


def conteudo(nome: str) -> str:
    return resources.files("firma.modelos").joinpath("claude", f"{nome}.md").read_text(encoding="utf-8")


def instalar(forcar: bool = False) -> list[tuple[str, str]]:
    """Grava cada comando em ~/.claude/commands. Devolve (nome, situação) para cada um.

    Situação: "instalado", "atualizado", "igual" ou "editado" (existe e difere; só sobrescreve com forcar).
    """
    destino = pasta_comandos()
    destino.mkdir(parents=True, exist_ok=True)
    resultado = []
    for nome in COMANDOS:
        novo = conteudo(nome)
        caminho = destino / f"{nome}.md"
        if not caminho.exists():
            caminho.write_text(novo, encoding="utf-8")
            resultado.append((nome, "instalado"))
        elif caminho.read_text(encoding="utf-8") == novo:
            resultado.append((nome, "igual"))
        elif forcar:
            caminho.write_text(novo, encoding="utf-8")
            resultado.append((nome, "atualizado"))
        else:
            resultado.append((nome, "editado"))
    return resultado
