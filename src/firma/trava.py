"""A trava do `firma run` (~/.firma/run.lock) e o pedido de parada.

A trava vale enquanto o run a renova. Renovar só toca o mtime, no máximo a cada 15s,
e é chamado a cada evento das sessões e durante os testes/lint.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from firma.registro import home_firma

VALIDADE_S = 30 * 60
INTERVALO_RENOVACAO_S = 15

_ultima_renovacao = 0.0


def arquivo_parar() -> Path:
    return home_firma() / "parar"


def arquivo_trava() -> Path:
    return home_firma() / "run.lock"


def pedir_parada() -> None:
    arquivo_parar().parent.mkdir(parents=True, exist_ok=True)
    arquivo_parar().write_text(datetime.now().isoformat(), encoding="utf-8")


def trava_ativa(validade_s: int = VALIDADE_S) -> dict | None:
    """Velha demais = o run morreu sem limpar."""
    caminho = arquivo_trava()
    if not caminho.exists():
        return None
    if time.time() - caminho.stat().st_mtime > validade_s:
        return None
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def criar_trava() -> None:
    global _ultima_renovacao
    arquivo_trava().parent.mkdir(parents=True, exist_ok=True)
    dados = {"pid": os.getpid(), "inicio": datetime.now().isoformat(timespec="seconds")}
    arquivo_trava().write_text(json.dumps(dados), encoding="utf-8")
    _ultima_renovacao = time.monotonic()


def renovar_trava(forcar: bool = False) -> None:
    global _ultima_renovacao
    agora = time.monotonic()
    if not forcar and agora - _ultima_renovacao < INTERVALO_RENOVACAO_S:
        return
    caminho = arquivo_trava()
    if caminho.exists():
        try:
            os.utime(caminho, None)
        except OSError:
            pass
    _ultima_renovacao = agora


def soltar_trava() -> None:
    arquivo_trava().unlink(missing_ok=True)
