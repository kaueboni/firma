"""Sonda da quota: uma chamada mínima pelo Agent SDK para ver o que ele informa.

É o primeiro risco técnico da Firma: o orçamento semanal só pode ser calculado
direto se o SDK informar a utilização (`utilization`) da quota. Se não informar,
a Firma vai precisar estimar o gasto pelos tokens de cada execução.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

PROMPT = "Responda apenas com a palavra: ok"


@dataclass
class Sonda:
    fonte_credencial: str | None = None
    eventos: list[dict[str, Any]] = field(default_factory=list)
    resultado: dict[str, Any] | None = None


def para_datetime(ts: int | float | None) -> datetime | None:
    """`resetsAt` chega em segundos Unix; alguns clientes já mandaram em milissegundos."""
    if ts is None:
        return None
    if ts > 1e12:
        ts = ts / 1000
    return datetime.fromtimestamp(ts)


async def sondar() -> Sonda:
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        RateLimitEvent,
        ResultMessage,
        SystemMessage,
        query,
    )

    opcoes = ClaudeAgentOptions(tools=[], max_turns=1, setting_sources=[])
    sonda = Sonda()
    async for msg in query(prompt=PROMPT, options=opcoes):
        if isinstance(msg, SystemMessage) and msg.subtype == "init":
            sonda.fonte_credencial = msg.data.get("apiKeySource")
        elif isinstance(msg, RateLimitEvent):
            sonda.eventos.append(dict(msg.rate_limit_info.raw))
        elif isinstance(msg, ResultMessage):
            sonda.resultado = {
                "is_error": msg.is_error,
                "usage": msg.usage,
                "total_cost_usd": msg.total_cost_usd,
                "api_error_status": msg.api_error_status,
            }
    return sonda


def resumir(sonda: Sonda) -> tuple[list[str], list[str]]:
    """Devolve (linhas de detalhe, linhas de veredito)."""
    linhas: list[str] = []
    veredito: list[str] = []

    fonte = sonda.fonte_credencial
    linhas.append(f"Credencial usada: {fonte or 'desconhecida'}")
    if fonte not in (None, "none"):
        veredito.append(
            f"ATENÇÃO: a chamada usou uma chave de API ({fonte}), não a assinatura. "
            "Remova a chave do ambiente antes de rodar a Firma."
        )

    if not sonda.eventos:
        linhas.append("Nenhum rate_limit_event recebido.")
    for ev in sonda.eventos:
        reset = para_datetime(ev.get("resetsAt"))
        util = ev.get("utilization")
        partes = [
            f"status={ev.get('status')}",
            f"tipo={ev.get('rateLimitType')}",
            f"utilização={'—' if util is None else f'{util:.0%}'}",
        ]
        if reset:
            partes.append(f"volta={reset:%d/%m %H:%M}")
        linhas.append("rate_limit_event: " + " ".join(partes))
        janelas = ev.get("unifiedWindows")
        if janelas:
            linhas.append(f"  janelas: {json.dumps(janelas, ensure_ascii=False)}")

    if sonda.resultado and sonda.resultado.get("usage"):
        linhas.append(f"Tokens da chamada: {json.dumps(sonda.resultado['usage'], ensure_ascii=False)}")

    tem_utilizacao = any(
        ev.get("utilization") is not None or ev.get("unifiedWindows") for ev in sonda.eventos
    )
    if tem_utilizacao:
        veredito.append("A utilização vem preenchida: dá para calcular o orçamento semanal direto.")
    else:
        veredito.append(
            "A utilização não veio: a Firma vai estimar o gasto pelos tokens de cada execução "
            "(ou rode de novo quando a quota estiver mais usada; o campo costuma aparecer perto do limite)."
        )
    return linhas, veredito


def salvar(sonda: Sonda, pasta: Path | None = None) -> Path:
    pasta = pasta or Path.home() / ".firma" / "sondas"
    pasta.mkdir(parents=True, exist_ok=True)
    caminho = pasta / f"quota-{time.strftime('%Y%m%d-%H%M%S')}.json"
    caminho.write_text(json.dumps(asdict(sonda), ensure_ascii=False, indent=2), encoding="utf-8")
    return caminho
