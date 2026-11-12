"""As regras que valem para todo agente da Firma, aplicadas em código antes de cada ferramenta.

Roda como hook PreToolUse do Agent SDK: o modelo não tem como esquecer ou contornar.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath

COMANDOS_PROIBIDOS = [
    (r"\bgit\s+push\b", "push é feito pela Firma, nunca pelo agente"),
    (r"\bgit\s+commit\b", "o commit é feito pela Firma depois da auditoria"),
    (r"\bgit\s+(reset\s+--hard|clean|checkout|switch|rebase|merge|stash|branch\s+-[dD])\b", "comando git que muda branch ou descarta trabalho"),
    (r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f|\brm\s+-[a-zA-Z]*f[a-zA-Z]*r|Remove-Item\b.*-Recurse", "remoção recursiva"),
    (r"\bgh\s", "o GitHub é usado só pela Firma"),
    (r"(curl|wget|iwr|Invoke-WebRequest)\b.*\|\s*(sh|bash|iex|python)", "baixar e executar script"),
    (r"\b(npm|pnpm|yarn|bun)\s+(publish|login)\b|\btwine\b|\buv\s+publish\b", "publicação de pacote"),
    (
        r"\b(npm|pnpm|yarn|bun)\s+(install|i|add)\s+[^-\s]|\buv\s+add\b|\bpip\s+install\s+[^-\s]|\bpoetry\s+add\b",
        "dependência nova precisa ser perguntada (limite do advisor)",
    ),
    (r"(^|[\s'\"/\\])\.env(?!\.example)(\.[\w.-]+)?\b", "arquivos .env podem ter segredos"),
]

FERRAMENTAS_DE_ESCRITA = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
CAMPOS_DE_CAMINHO = ("file_path", "path", "notebook_path")


def _e_env(caminho: str) -> bool:
    nome = PureWindowsPath(caminho).name if "\\" in caminho else PurePosixPath(caminho).name
    return nome == ".env" or (nome.startswith(".env.") and nome != ".env.example")


def _dentro(caminho: str, raiz: Path) -> bool:
    p = Path(caminho)
    if not p.is_absolute():
        p = raiz / p
    try:
        p.resolve().relative_to(raiz.resolve())
        return True
    except ValueError:
        return False


def avaliar(
    ferramenta: str,
    entrada: dict,
    raiz: Path,
    pode_escrever: bool = True,
    escrita_so_em: str | None = None,
) -> str | None:
    """Devolve o motivo da recusa, ou None se a ação é permitida.

    raiz: a worktree da feature. pode_escrever: False para auditor e advisor.
    escrita_so_em: pasta relativa onde a escrita é permitida (o tech-lead só escreve a spec).
    """
    if ferramenta == "Bash":
        comando = str(entrada.get("command", ""))
        for padrao, motivo in COMANDOS_PROIBIDOS:
            if re.search(padrao, comando, re.IGNORECASE):
                return f"Bloqueado pela Firma: {motivo}."
        return None

    caminhos = [str(entrada[c]) for c in CAMPOS_DE_CAMINHO if entrada.get(c)]
    for caminho in caminhos:
        if _e_env(caminho):
            return "Bloqueado pela Firma: arquivos .env podem ter segredos."

    if ferramenta in FERRAMENTAS_DE_ESCRITA:
        if not pode_escrever:
            return "Bloqueado pela Firma: este papel não edita arquivos."
        for caminho in caminhos:
            if not _dentro(caminho, raiz):
                return "Bloqueado pela Firma: só é permitido escrever dentro da worktree da feature."
            if escrita_so_em and not _dentro(caminho, raiz / escrita_so_em):
                return f"Bloqueado pela Firma: este papel só escreve em {escrita_so_em}/."
    return None


def hook(raiz: Path, pode_escrever: bool = True, escrita_so_em: str | None = None, ao_bloquear=None):
    """Monta o callback PreToolUse para o Agent SDK."""

    async def _callback(entrada, tool_use_id, contexto):
        if entrada.get("hook_event_name") != "PreToolUse":
            return {}
        motivo = avaliar(entrada.get("tool_name", ""), entrada.get("tool_input") or {}, raiz, pode_escrever, escrita_so_em)
        if motivo is None:
            return {}
        if ao_bloquear:
            ao_bloquear(entrada.get("tool_name", ""), motivo)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": motivo,
            }
        }

    return _callback
