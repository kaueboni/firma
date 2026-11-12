"""Uma sessão de agente pelo Claude Agent SDK, com a guarda da Firma, o feed e a leitura da quota."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class Quota:
    """Último retrato da quota da conta, vindo dos rate_limit_event."""

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
    ferramentas: list[str] | None = None  # None = ferramentas padrão do Claude Code
    modelo: str | None = None
    advisor: str | None = None  # modelo do advisor do Claude Code dentro desta sessão
    permissao: str = "bypassPermissions"
    max_turnos: int | None = None


@dataclass
class Resposta:
    texto: str = ""
    session_id: str | None = None
    erro: str | None = None
    sem_quota: bool = False
    bloqueios: list[str] = field(default_factory=list)


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


def _resumo_ferramenta(nome: str, entrada: dict, cwd: Path) -> str:
    if nome == "Bash":
        return f"Bash: {str(entrada.get('command', ''))[:120]}"
    for campo in ("file_path", "path", "notebook_path"):
        if entrada.get(campo):
            return f"{nome} {caminho_curto(entrada[campo], cwd)}"
    for campo in ("pattern", "url", "query"):
        if entrada.get(campo):
            return f"{nome} {entrada[campo]}"
    return nome


def _total_tokens(uso: dict | None) -> int:
    if not uso:
        return 0
    campos = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    return sum(int(uso.get(c) or 0) for c in campos)


class _Feed:
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


async def rodar(pedido: Pedido, registro: Callable, quota: Quota, detalhe: bool = False) -> Resposta:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        HookMatcher,
        RateLimitEvent,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ToolUseBlock,
        query,
    )

    from firma.guarda import hook

    resposta = Resposta()

    def ao_bloquear(ferramenta: str, motivo: str) -> None:
        resposta.bloqueios.append(motivo)
        registro(f"{ferramenta} recusado: {motivo}", prd=pedido.prd, papel=pedido.papel)

    opcoes = ClaudeAgentOptions(
        cwd=str(pedido.cwd),
        resume=pedido.resume,
        permission_mode=pedido.permissao,
        setting_sources=["project"],
        system_prompt={"type": "preset", "preset": "claude_code"},
        hooks={
            "PreToolUse": [
                HookMatcher(
                    matcher=None,
                    hooks=[hook(pedido.cwd, pedido.pode_escrever, pedido.escrita_so_em, ao_bloquear)],
                    on_failure="block",
                )
            ]
        },
    )
    if pedido.ferramentas is not None:
        opcoes.tools = pedido.ferramentas
    if pedido.modelo:
        opcoes.model = pedido.modelo
    if pedido.advisor:
        opcoes.settings = json.dumps({"advisorModel": pedido.advisor})
    if pedido.max_turnos:
        opcoes.max_turns = pedido.max_turnos

    textos: list[str] = []
    feed = _Feed(registro, pedido.prd, pedido.papel)
    inicio, semanal_antes, tokens = time.monotonic(), quota.semanal, 0
    async for msg in query(prompt=pedido.prompt, options=opcoes):
        if isinstance(msg, SystemMessage) and msg.subtype == "init":
            resposta.session_id = msg.data.get("session_id") or resposta.session_id
            if msg.data.get("apiKeySource") not in (None, "none"):
                resposta.erro = "a sessão usaria uma chave de API, não a assinatura"
                break
        elif isinstance(msg, RateLimitEvent):
            quota.atualizar(dict(msg.rate_limit_info.raw))
            if quota.rejeitado:
                resposta.sem_quota = True
        elif isinstance(msg, AssistantMessage):
            for bloco in msg.content:
                if isinstance(bloco, ToolUseBlock):
                    feed(_resumo_ferramenta(bloco.name, bloco.input, pedido.cwd))
                elif isinstance(bloco, TextBlock) and bloco.text.strip():
                    textos.append(bloco.text)
                    if detalhe:
                        feed(bloco.text.strip()[:400])
        elif isinstance(msg, ResultMessage):
            resposta.session_id = msg.session_id or resposta.session_id
            tokens = _total_tokens(msg.usage)
            if msg.is_error:
                resposta.erro = "; ".join(msg.errors or []) or f"erro na sessão ({msg.subtype})"
                if msg.api_error_status == 429:
                    resposta.sem_quota = True
            if msg.result:
                textos.append(msg.result)
    feed.fechar()
    minutos = (time.monotonic() - inicio) / 60
    def pct(v):
        return "?" if v is None else f"{v:.0%}"
    registro(
        f"terminou · {minutos:.0f} min · {tokens / 1000:.0f}k tokens · semana {pct(semanal_antes)} → {pct(quota.semanal)}",
        prd=pedido.prd, papel=pedido.papel, tokens=tokens, minutos=round(minutos, 1),
    )
    resposta.texto = textos[-1] if textos else ""
    return resposta
