"""O que é comum a qualquer runtime de agente: o pedido, a resposta, a quota e o feed."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

PAPEIS = ("tech-lead", "advisor", "developer", "auditor")


@dataclass
class Quota:
    """Último retrato da quota da assinatura Claude, vindo dos rate_limit_event."""

    semanal: float | None = None
    cinco_horas: float | None = None
    volta_semanal: int | None = None
    volta_cinco_horas: int | None = None
    rejeitado: bool = False

    def atualizar(self, info: dict) -> None:
        janelas = info.get("unifiedWindows") or {}
        if isinstance(janelas, dict):
            sete = janelas.get("seven_day") or {}
            cinco = janelas.get("five_hour") or {}
            if sete.get("utilization") is not None:
                self.semanal = float(sete["utilization"])
                self.volta_semanal = sete.get("resetsAt")
            if cinco.get("utilization") is not None:
                self.cinco_horas = float(cinco["utilization"])
                self.volta_cinco_horas = cinco.get("resetsAt")
        tipo, util = info.get("rateLimitType"), info.get("utilization")
        if util is not None and tipo == "seven_day":
            self.semanal = float(util)
        if util is not None and tipo == "five_hour":
            self.cinco_horas = float(util)
        self.rejeitado = info.get("status") == "rejected"


@dataclass
class Pedido:
    papel: str
    prompt: str
    cwd: Path
    prd: str = ""
    resume: str | None = None
    pode_escrever: bool = True
    escrita_so_em: str | None = None
    # pastas relativas à worktree onde este papel não escreve (ex.: .claude, docs/firma/<PRD>)
    escrita_proibida: list[str] = field(default_factory=list)
    ferramentas: list[str] | None = None  # None = ferramentas padrão do runtime
    provider: str = "anthropic"
    modelo: str | None = None
    permissao: str = "bypassPermissions"
    max_turnos: int | None = None


@dataclass
class Resposta:
    texto: str = ""
    session_id: str | None = None
    erro: str | None = None
    tipo_erro: str | None = None  # um de runtime.erros.TIPOS
    sem_quota: bool = False
    bloqueios: list[str] = field(default_factory=list)
    tokens: int = 0
    provider: str = ""
    modelo: str = ""


def caminho_curto(caminho: str, cwd: Path) -> str:
    """`C:\\...\\worktrees\\PRD-001\\agent\\app.py` vira `agent/app.py`; bibliotecas viram `[lib] pacote/x.py`."""
    texto = str(caminho).replace("\\", "/")
    for marca, rotulo in (("site-packages/", "[lib] "), ("node_modules/", "[npm] ")):
        if marca in texto:
            return rotulo + texto.split(marca, 1)[1]
    bases = [cwd]
    if cwd.parent.name == "worktrees" and len(cwd.parents) >= 3:
        bases.append(cwd.parents[2])  # o repositório principal, por trás da worktree
    for base in bases:
        prefixo = str(base).replace("\\", "/").rstrip("/") + "/"
        if texto.lower().startswith(prefixo.lower()):
            return texto[len(prefixo):]
        if texto.rstrip("/").lower() == prefixo.rstrip("/").lower():
            return "."
    return texto


class Feed:
    """Mostra cada ação na hora e junta repetições seguidas numa linha "↳ mais N vezes"."""

    def __init__(self, registro: Callable, prd: str, papel: str):
        self.registro, self.prd, self.papel = registro, prd, papel
        self.ultimo, self.repeticoes = None, 0

    def __call__(self, texto: str) -> None:
        if texto == self.ultimo:
            self.repeticoes += 1
            return
        self.fechar()
        self.ultimo = texto
        self.registro(texto, prd=self.prd, papel=self.papel)

    def fechar(self) -> None:
        if self.repeticoes:
            self.registro(f"  ↳ mais {self.repeticoes} vez(es)", prd=self.prd, papel=self.papel)
        self.ultimo, self.repeticoes = None, 0


_Feed = Feed  # nome antigo
