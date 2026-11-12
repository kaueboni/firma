"""Uma sessão de agente pelo Claude Agent SDK, com a guarda da Firma, o feed e a leitura da quota."""

from __future__ import annotations

import json
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


def _resumo_ferramenta(nome: str, entrada: dict) -> str:
    if nome == "Bash":
        return f"Bash: {str(entrada.get('command', ''))[:120]}"
    for campo in ("file_path", "path", "pattern", "url"):
        if entrada.get(campo):
            return f"{nome} {entrada[campo]}"
    return nome


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
                    registro(_resumo_ferramenta(bloco.name, bloco.input), prd=pedido.prd, papel=pedido.papel)
                elif isinstance(bloco, TextBlock) and bloco.text.strip():
                    textos.append(bloco.text)
                    if detalhe:
                        registro(bloco.text.strip()[:400], prd=pedido.prd, papel=pedido.papel)
        elif isinstance(msg, ResultMessage):
            resposta.session_id = msg.session_id or resposta.session_id
            if msg.is_error:
                resposta.erro = "; ".join(msg.errors or []) or f"erro na sessão ({msg.subtype})"
                if msg.api_error_status == 429:
                    resposta.sem_quota = True
            if msg.result:
                textos.append(msg.result)
    resposta.texto = textos[-1] if textos else ""
    return resposta
