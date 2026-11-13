"""A config que a Firma injeta no OpenCode: um agente por papel, as permissões e o plugin da guarda.

Tudo resolve em allow ou deny (nunca ask): uma pergunta de permissão travaria uma sessão sem
ninguém para responder. O que depende do PRD (pasta da spec, worktree) vai por sessão: nas
regras de permissão da sessão e no escopo que o plugin lê.
"""

from __future__ import annotations

import json
import os
from importlib import resources
from pathlib import Path

from firma import guarda

PREFIXO_AGENTE = "firma-"
LER_ENV = {"*": "allow", "*.env": "deny", "*.env.*": "deny", "*.env.example": "allow"}

_COMUM = {
    "read": LER_ENV,
    "glob": "allow",
    "grep": "allow",
    "list": "allow",
    "skill": "allow",
    "todowrite": "allow",
    "lsp": "allow",
    "task": "deny",
    "external_directory": "deny",
    "webfetch": "deny",
    "websearch": "deny",
    "question": "deny",
    "doom_loop": "deny",
}

PERMISSOES = {
    "tech-lead": {**_COMUM, "edit": "allow", "bash": "deny"},  # a pasta da spec é imposta por sessão e pelo plugin
    "advisor": {**_COMUM, "edit": "deny", "bash": "deny"},
    "auditor": {**_COMUM, "edit": "deny", "bash": "deny"},
    "developer": {**_COMUM, "edit": "allow", "bash": "allow"},  # o plugin filtra os comandos
}

DESCRICOES = {
    "tech-lead": "Firma: escreve spec e plano de um PRD",
    "advisor": "Firma: revisa spec e diagnostica tarefas (só leitura)",
    "auditor": "Firma: audita uma tarefa (só leitura)",
    "developer": "Firma: implementa uma tarefa com TDD",
}


def nome_agente(papel: str) -> str:
    return PREFIXO_AGENTE + papel


def caminho_plugin() -> Path:
    return Path(str(resources.files("firma").joinpath("opencode_plugin", "guarda.ts")))


def config(plugin: Path | None = None) -> dict:
    plugin = plugin or caminho_plugin()
    return {
        "$schema": "https://opencode.ai/config.json",
        "plugin": [plugin.resolve().as_uri()],
        "autoupdate": False,
        "share": "disabled",
        "permission": {**_COMUM, "edit": "deny", "bash": "deny"},
        "agent": {
            nome_agente(papel): {"mode": "primary", "description": DESCRICOES[papel], "permission": perm}
            for papel, perm in PERMISSOES.items()
        },
    }


def regras_da_sessao(papel: str, pode_escrever: bool, escrita_so_em: str | None, escrita_proibida: list[str]) -> list[dict]:
    """Regras de permissão por sessão (formato PermissionRule), somadas às do agente."""
    regras = [
        {"permission": "external_directory", "pattern": "*", "action": "deny"},
        {"permission": "doom_loop", "pattern": "*", "action": "deny"},
        {"permission": "task", "pattern": "*", "action": "deny"},
        {"permission": "question", "pattern": "*", "action": "deny"},
    ]
    if not pode_escrever:
        regras.append({"permission": "edit", "pattern": "*", "action": "deny"})
    elif escrita_so_em:
        regras += [
            {"permission": "edit", "pattern": "*", "action": "deny"},
            {"permission": "edit", "pattern": f"{escrita_so_em}/*", "action": "allow"},
        ]
    for pasta in [".git", ".opencode", ".claude", *escrita_proibida]:
        if pode_escrever:
            regras.append({"permission": "edit", "pattern": f"{pasta}/*", "action": "deny"})
    if papel != "developer":
        regras.append({"permission": "bash", "pattern": "*", "action": "deny"})
    return regras


class EstadoPlugin:
    """O JSON que o plugin lê a cada ferramenta: regras de shell e o escopo de cada sessão."""

    def __init__(self, caminho: Path):
        self.caminho = caminho
        self.sessoes: dict[str, dict] = {}
        self._gravar()

    def registrar(self, session_id: str, papel: str, raiz: Path, pode_escrever: bool, escrita_so_em: str | None, escrita_proibida: list[str]) -> None:
        self.sessoes[session_id] = {
            "papel": papel,
            "raiz": str(raiz),
            "pode_escrever": pode_escrever,
            "escrita_so_em": escrita_so_em,
            "escrita_proibida": list(escrita_proibida),
        }
        self._gravar()

    def _gravar(self) -> None:
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.caminho.with_name(self.caminho.name + ".tmp")
        dados = {"regras": guarda.regras_para_plugin(), "sessoes": self.sessoes}
        tmp.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.caminho)
