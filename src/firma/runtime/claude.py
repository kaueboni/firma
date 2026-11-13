"""Uma sessão de agente no Claude, pelo Claude Agent SDK e pela assinatura (nunca chave de API)."""

from __future__ import annotations

import time
from typing import Callable

import anyio

from firma.runtime import erros
from firma.runtime.base import Feed, Pedido, Quota, Resposta, caminho_curto
from firma.trava import renovar_trava


def _resumo_ferramenta(nome: str, entrada: dict, cwd) -> str:
    if nome == "Bash":
        return f"Bash: {str(entrada.get('command', ''))[:120]}"
    for campo in ("file_path", "path", "notebook_path"):
        if entrada.get(campo):
            return f"{nome} {caminho_curto(entrada[campo], cwd)}"
    for campo in ("pattern", "url", "query", "skill"):
        if entrada.get(campo):
            return f"{nome} {entrada[campo]}"
    return nome


def _total_tokens(uso: dict | None) -> int:
    if not uso:
        return 0
    campos = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    return sum(int(uso.get(c) or 0) for c in campos)


def _pct(v):
    return "?" if v is None else f"{v:.0%}"


async def _uma_vez(pedido: Pedido, registro: Callable, quota: Quota, detalhe: bool, feed: Feed) -> tuple[Resposta, int | None]:
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

    resposta = Resposta(provider="anthropic", modelo=pedido.modelo or "")
    status_http: int | None = None

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
                    hooks=[hook(pedido.cwd, pedido.pode_escrever, pedido.escrita_so_em, ao_bloquear, pedido.escrita_proibida)],
                    on_failure="block",
                )
            ]
        },
    )
    if pedido.ferramentas is not None:
        opcoes.tools = pedido.ferramentas
    if pedido.modelo:
        opcoes.model = pedido.modelo
    if pedido.max_turnos:
        opcoes.max_turns = pedido.max_turnos

    textos: list[str] = []
    async for msg in query(prompt=pedido.prompt, options=opcoes):
        renovar_trava()
        if isinstance(msg, SystemMessage) and msg.subtype == "init":
            resposta.session_id = msg.data.get("session_id") or resposta.session_id
            if msg.data.get("apiKeySource") not in (None, "none"):
                resposta.erro = "a sessão usaria uma chave de API, não a assinatura"
                resposta.tipo_erro = erros.AUTENTICACAO
                break
        elif isinstance(msg, RateLimitEvent):
            quota.atualizar(dict(msg.rate_limit_info.raw))
            if quota.rejeitado:
                resposta.sem_quota = True
                resposta.tipo_erro = erros.QUOTA
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
            resposta.tokens += _total_tokens(msg.usage)
            if msg.is_error:
                resposta.erro = "; ".join(msg.errors or []) or f"erro na sessão ({msg.subtype})"
                status_http = getattr(msg, "api_error_status", None)
                if not resposta.sem_quota:
                    resposta.tipo_erro = erros.classificar(status_http, resposta.erro)
            if msg.result:
                textos.append(msg.result)
    resposta.texto = textos[-1] if textos else ""
    if resposta.tipo_erro == erros.QUOTA:
        resposta.sem_quota = True
    return resposta, status_http


async def rodar(pedido: Pedido, registro: Callable, quota: Quota, detalhe: bool = False) -> Resposta:
    feed = Feed(registro, pedido.prd, pedido.papel)
    inicio, semanal_antes, tokens = time.monotonic(), quota.semanal, 0
    tentativas = list(erros.ESPERAS_TRANSITORIO)
    while True:
        resposta, status = await _uma_vez(pedido, registro, quota, detalhe, feed)
        tokens += resposta.tokens
        if resposta.tipo_erro == erros.TRANSITORIO and not resposta.sem_quota:
            if tentativas:
                espera = tentativas.pop(0)
                feed.fechar()
                registro(f"erro transitório ({status or '?'}): nova tentativa em {espera}s", prd=pedido.prd, papel=pedido.papel)
                await anyio.sleep(espera)
                if resposta.session_id:
                    pedido.resume = resposta.session_id
                continue
            if status == 429:  # 429 que não passa depois das tentativas = limite da conta
                resposta.tipo_erro, resposta.sem_quota = erros.QUOTA, True
        break
    feed.fechar()
    resposta.tokens = tokens
    minutos = (time.monotonic() - inicio) / 60
    registro(
        f"terminou · {minutos:.0f} min · {tokens / 1000:.0f}k tokens · semana {_pct(semanal_antes)} → {_pct(quota.semanal)}",
        prd=pedido.prd, papel=pedido.papel, tokens=tokens, minutos=round(minutos, 1), provider="anthropic",
    )
    return resposta
