"""Escolhe o runtime de cada sessão pelo provedor: Claude pelo Agent SDK, o resto pelo OpenCode.

O servidor do OpenCode só sobe na primeira sessão que precisa dele e é encerrado ao sair
(inclusive em Ctrl+C), abortando a sessão que estiver em andamento.
"""

from __future__ import annotations

from typing import Callable

from firma.runtime.base import Pedido, Quota, Resposta


class Roteador:
    def __init__(self, registro: Callable, detalhe: bool = False):
        self.registro, self.detalhe = registro, detalhe
        self._opencode = None

    async def __aenter__(self) -> "Roteador":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.fechar()

    async def fechar(self) -> None:
        if self._opencode is not None:
            await self._opencode.fechar()
            self._opencode = None

    async def __call__(self, pedido: Pedido, quota: Quota) -> Resposta:
        if pedido.provider == "anthropic":
            from firma.runtime import claude

            return await claude.rodar(pedido, self.registro, quota, self.detalhe)
        if self._opencode is None:
            from firma.runtime.opencode import HostOpencode

            self._opencode = HostOpencode(self.registro, self.detalhe)
        return await self._opencode.rodar(pedido)
