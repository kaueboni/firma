"""Leitura do .firma/config.toml de um projeto, com os valores padrão."""

from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

PADRAO = {
    "projeto": {"nome": "", "branch_principal": "main", "github": ""},
    "partes": [],
    "quota": {"teto_semanal": 0.6, "janela": ""},
    "execucao": {
        # Modo de permissão das sessões. As regras da Firma (guarda.py) valem em qualquer modo.
        "permissao": "bypassPermissions",
        "rodadas": 3,
    },
    "modelos": {"trabalho": "", "advisor": "opus"},
}


def carregar(raiz: Path) -> dict:
    caminho = raiz / ".firma" / "config.toml"
    dados = tomllib.loads(caminho.read_text(encoding="utf-8-sig"))
    final: dict = {}
    for secao, padrao in PADRAO.items():
        valor = dados.get(secao, padrao)
        if isinstance(padrao, dict) and isinstance(valor, dict):
            final[secao] = {**padrao, **valor}
        else:
            final[secao] = valor
    return final
