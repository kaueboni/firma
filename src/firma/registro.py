"""Lista global de projetos da Firma (em ~/.firma/projetos.json).

A quota é da conta, não do projeto: um registro único deixa a Firma dividir
o mesmo orçamento entre todos os projetos desta máquina.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path


def home_firma() -> Path:
    return Path(os.environ.get("FIRMA_HOME") or Path.home() / ".firma")


def _arquivo() -> Path:
    return home_firma() / "projetos.json"


def listar() -> list[dict]:
    try:
        dados = json.loads(_arquivo().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return dados if isinstance(dados, list) else []


def registrar(raiz: Path, nome: str) -> bool:
    """Registra o projeto. Devolve False se ele já estava registrado."""
    caminho = str(raiz.resolve())
    projetos = listar()
    if any(p.get("caminho") == caminho for p in projetos):
        return False
    projetos.append({"nome": nome, "caminho": caminho, "registrado_em": datetime.now().isoformat(timespec="seconds")})
    arquivo = _arquivo()
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    arquivo.write_text(json.dumps(projetos, ensure_ascii=False, indent=2), encoding="utf-8")
    return True
