"""As regras que valem para todo agente da Firma, aplicadas em código antes de cada ferramenta.

No Claude roda como hook PreToolUse do Agent SDK; no OpenCode as mesmas regras vão para o
plugin da Firma (exportadas por `regras_para_plugin`). O modelo não tem como esquecer ou contornar.
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
    (r"\.local[/\\]share[/\\]opencode|\.config[/\\]opencode|\.claude[/\\]\.credentials", "credenciais dos agentes"),
]

FERRAMENTAS_DE_ESCRITA = {"Edit", "Write", "MultiEdit", "NotebookEdit", "edit", "write", "patch", "multiedit"}
FERRAMENTAS_DE_SHELL = {"Bash", "bash", "shell"}
CAMPOS_DE_CAMINHO = ("file_path", "path", "notebook_path", "filePath")

# Lugares de credenciais dos agentes: nenhum papel lê nem escreve.
_CREDENCIAIS = re.compile(r"[/\\]\.local[/\\]share[/\\]opencode([/\\]|$)|[/\\]\.config[/\\]opencode([/\\]|$)|[/\\]\.claude[/\\]\.credentials", re.IGNORECASE)

# Pastas da worktree em que nenhum papel escreve (a .git e os ambientes ligados do repositório principal).
SEMPRE_PROIBIDO = (".git", ".opencode", ".claude")


def _e_env(caminho: str) -> bool:
    nome = PureWindowsPath(caminho).name if "\\" in caminho else PurePosixPath(caminho).name
    return nome == ".env" or (nome.startswith(".env.") and nome != ".env.example")


def _absoluto(caminho: str, raiz: Path) -> Path:
    p = Path(caminho)
    return p if p.is_absolute() else raiz / p


def _dentro(caminho: str, raiz: Path) -> bool:
    try:
        _absoluto(caminho, raiz).resolve().relative_to(raiz.resolve())
        return True
    except ValueError:
        return False


def _dentro_logico(caminho: str, raiz: Path, pasta: str) -> bool:
    """Como _dentro, mas sem seguir junções/symlinks: `.claude` ligado continua sendo `.claude`."""
    import os

    alvo = os.path.normcase(os.path.normpath(str(_absoluto(caminho, raiz))))
    base = os.path.normcase(os.path.normpath(str(raiz / pasta)))
    return alvo == base or alvo.startswith(base + os.sep)


def avaliar(
    ferramenta: str,
    entrada: dict,
    raiz: Path,
    pode_escrever: bool = True,
    escrita_so_em: str | None = None,
    escrita_proibida: list[str] | tuple[str, ...] = (),
) -> str | None:
    """Devolve o motivo da recusa, ou None se a ação é permitida.

    raiz: a worktree da feature. pode_escrever: False para auditor e advisor.
    escrita_so_em: pasta relativa onde a escrita é permitida (o tech-lead só escreve a spec).
    escrita_proibida: pastas relativas onde este papel não escreve (o developer não mexe na spec).
    """
    if ferramenta in FERRAMENTAS_DE_SHELL:
        comando = str(entrada.get("command", ""))
        for padrao, motivo in COMANDOS_PROIBIDOS:
            if re.search(padrao, comando, re.IGNORECASE):
                return f"Bloqueado pela Firma: {motivo}."
        return None

    caminhos = [str(entrada[c]) for c in CAMPOS_DE_CAMINHO if entrada.get(c)]
    for caminho in caminhos:
        if _e_env(caminho):
            return "Bloqueado pela Firma: arquivos .env podem ter segredos."
        if _CREDENCIAIS.search(str(_absoluto(caminho, raiz))):
            return "Bloqueado pela Firma: credenciais dos agentes."

    if ferramenta in FERRAMENTAS_DE_ESCRITA:
        if not pode_escrever:
            return "Bloqueado pela Firma: este papel não edita arquivos."
        for caminho in caminhos:
            for pasta in SEMPRE_PROIBIDO:
                if _dentro_logico(caminho, raiz, pasta):
                    return f"Bloqueado pela Firma: ninguém escreve em {pasta}/."
            if not _dentro(caminho, raiz):
                return "Bloqueado pela Firma: só é permitido escrever dentro da worktree da feature."
            if escrita_so_em and not _dentro(caminho, raiz / escrita_so_em):
                return f"Bloqueado pela Firma: este papel só escreve em {escrita_so_em}/."
            for pasta in escrita_proibida:
                if _dentro_logico(caminho, raiz, pasta):
                    return f"Bloqueado pela Firma: este papel não escreve em {pasta}/."
    return None


def hook(raiz: Path, pode_escrever: bool = True, escrita_so_em: str | None = None, ao_bloquear=None, escrita_proibida=()):
    """Monta o callback PreToolUse para o Agent SDK."""

    async def _callback(entrada, tool_use_id, contexto):
        if entrada.get("hook_event_name") != "PreToolUse":
            return {}
        motivo = avaliar(
            entrada.get("tool_name", ""), entrada.get("tool_input") or {}, raiz, pode_escrever, escrita_so_em, escrita_proibida
        )
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


def regras_para_plugin() -> list[dict]:
    """As regras de shell no formato que o plugin do OpenCode lê (regex compatível com JS)."""
    return [{"padrao": padrao, "motivo": motivo} for padrao, motivo in COMANDOS_PROIBIDOS]
