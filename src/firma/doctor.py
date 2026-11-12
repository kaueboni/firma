"""Checagens do ambiente: o que a Firma precisa para rodar nesta máquina."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Mapping

OK = "ok"
AVISO = "aviso"
FALHA = "falha"


@dataclass
class Checagem:
    nome: str
    estado: str  # OK, AVISO ou FALHA
    detalhe: str


Executor = Callable[[list[str]], subprocess.CompletedProcess]


def _executar(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def checar_python(versao: tuple[int, int] | None = None) -> Checagem:
    versao = versao or sys.version_info[:2]
    texto = f"{versao[0]}.{versao[1]}"
    if versao >= (3, 10):
        return Checagem("Python", OK, texto)
    return Checagem("Python", FALHA, f"{texto} (precisa de 3.10 ou mais)")


def checar_comando(nome: str, cmd: list[str], dica: str, executar: Executor = _executar) -> Checagem:
    if shutil.which(cmd[0]) is None:
        return Checagem(nome, FALHA, f"`{cmd[0]}` não encontrado. {dica}")
    try:
        r = executar(cmd)
    except (OSError, subprocess.SubprocessError) as e:
        return Checagem(nome, FALHA, f"erro ao rodar `{' '.join(cmd)}`: {e}")
    if r.returncode != 0:
        return Checagem(nome, FALHA, dica)
    saida = (r.stdout or r.stderr or "").strip().splitlines()
    return Checagem(nome, OK, saida[0] if saida else "ok")


def checar_git(executar: Executor = _executar) -> Checagem:
    return checar_comando("git", ["git", "--version"], "Instale o git.", executar)


def checar_gh(executar: Executor = _executar) -> Checagem:
    c = checar_comando("GitHub CLI", ["gh", "auth", "status"], "Rode `gh auth login`.", executar)
    if c.estado == OK:
        c.detalhe = "autenticado"
    return c


def checar_credencial(env: Mapping[str, str] | None = None) -> Checagem:
    """A Firma precisa usar a assinatura, nunca uma chave de API cobrada por token.

    O Claude Code dá prioridade à chave de API quando ela existe no ambiente,
    então uma ANTHROPIC_API_KEY esquecida faria tudo ser cobrado por token.
    """
    env = os.environ if env is None else env
    if env.get("ANTHROPIC_API_KEY"):
        return Checagem(
            "Credencial",
            FALHA,
            "ANTHROPIC_API_KEY está definida: o uso seria cobrado por token, não pela assinatura. "
            "Remova a variável do ambiente.",
        )
    if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return Checagem("Credencial", OK, "token da assinatura (CLAUDE_CODE_OAUTH_TOKEN)")
    return Checagem("Credencial", OK, "login do Claude Code (confirme com `firma doctor --quota`)")


def checar_sdk() -> Checagem:
    try:
        import claude_agent_sdk
    except ImportError:
        return Checagem("Claude Agent SDK", FALHA, "pacote não instalado")
    return Checagem("Claude Agent SDK", OK, getattr(claude_agent_sdk, "__version__", "instalado"))


def rodar_checagens() -> list[Checagem]:
    return [checar_python(), checar_git(), checar_gh(), checar_sdk(), checar_credencial()]
