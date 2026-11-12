"""Registro do que a Firma faz: um arquivo JSONL por dia e o feed no terminal."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from firma.registro import home_firma


def pasta_logs() -> Path:
    return home_firma() / "logs"


def arquivo_do_dia(dia: datetime | None = None) -> Path:
    return pasta_logs() / f"{(dia or datetime.now()):%Y-%m-%d}.jsonl"


def formatar(ev: dict) -> str:
    hora = ev.get("hora", "")[11:16]
    origem = " · ".join(x for x in (ev.get("prd"), ev.get("papel")) if x)
    return f"{hora} {origem:24} {ev.get('texto', '')}"


class Registro:
    def __init__(self, mostrar: Callable[[str], None] | None = print, arquivo: Path | None = None):
        self.mostrar = mostrar
        self.arquivo = arquivo or arquivo_do_dia()
        self.arquivo.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, texto: str, prd: str = "", papel: str = "", **dados) -> dict:
        ev = {"hora": datetime.now().isoformat(timespec="seconds"), "prd": prd, "papel": papel, "texto": texto, **dados}
        with self.arquivo.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        if self.mostrar:
            self.mostrar(formatar(ev))
        return ev


def seguir(parar: Callable[[], bool] = lambda: False, intervalo: float = 1.0, desde_o_fim: int = 20):
    """Gera linhas formatadas do log do dia, como um `tail -f` (troca de arquivo na virada do dia)."""
    atual = arquivo_do_dia()
    posicao = 0
    if atual.exists():
        linhas = atual.read_text(encoding="utf-8").splitlines()
        for linha in linhas[-desde_o_fim:]:
            yield formatar(json.loads(linha))
        posicao = atual.stat().st_size
    while not parar():
        hoje = arquivo_do_dia()
        if hoje != atual:
            atual, posicao = hoje, 0
        if atual.exists() and atual.stat().st_size > posicao:
            with atual.open("r", encoding="utf-8") as f:
                f.seek(posicao)
                for linha in f:
                    if linha.strip():
                        yield formatar(json.loads(linha))
                posicao = f.tell()
        time.sleep(intervalo)
