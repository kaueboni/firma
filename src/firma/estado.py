"""O estado de cada PRD em execução (.firma/estado/<PRD>.json), com escrita atômica.

Schema v2. Um JSON sem `schema_version` veio da Firma v1 e é migrado ao carregar.
Um JSON corrompido é erro: nunca vira estado vazio (isso refaria a spec e as tarefas).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

SCHEMA = 2


class EstadoCorrompido(RuntimeError):
    pass


def chave_sessao(papel: str, provider: str, modelo: str | None) -> str:
    """Sessões nunca são reaproveitadas entre provedores ou modelos diferentes."""
    return f"{papel}:{provider}:{modelo or ''}"


@dataclass
class EstadoPrd:
    schema_version: int = SCHEMA
    fase: str = "spec"  # spec → tarefas → fim
    feitas: list[str] = field(default_factory=list)
    sessoes: dict[str, str] = field(default_factory=dict)  # chave_sessao → session_id
    pr: str = ""
    decisoes: list[str] = field(default_factory=list)
    # retomada exata de uma tarefa interrompida
    tarefa_ativa: str | None = None
    rodada_atual: int = 1
    ultimo_retorno: str | None = None
    historico_rodadas: list[str] = field(default_factory=list)
    provedores_esgotados: list[str] = field(default_factory=list)

    @staticmethod
    def caminho(raiz: Path, prd_id: str) -> Path:
        return raiz / ".firma" / "estado" / f"{prd_id}.json"

    @classmethod
    def de_dict(cls, dados: dict) -> "EstadoPrd":
        if not isinstance(dados, dict):
            raise EstadoCorrompido("o estado não é um objeto JSON")
        conhecidos = {f.name for f in fields(cls)}
        limpos = {k: v for k, v in dados.items() if k in conhecidos}
        if "schema_version" not in dados:  # v1 (Firma Python antes do v2)
            antigas = limpos.get("sessoes") or {}
            # As sessões da v1 eram por papel, todas no Claude; o modelo exato não foi guardado.
            limpos["sessoes"] = {chave_sessao(papel, "anthropic", ""): sid for papel, sid in antigas.items() if sid and ":" not in papel}
        limpos["schema_version"] = SCHEMA
        return cls(**limpos)

    @classmethod
    def carregar(cls, raiz: Path, prd_id: str) -> "EstadoPrd":
        c = cls.caminho(raiz, prd_id)
        if not c.exists():
            return cls()
        try:
            dados = json.loads(c.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise EstadoCorrompido(f"{c} está corrompido ({e}); corrija ou apague o arquivo para recomeçar o PRD") from e
        try:
            return cls.de_dict(dados)
        except TypeError as e:
            raise EstadoCorrompido(f"{c} tem campos inválidos ({e})") from e

    def salvar(self, raiz: Path, prd_id: str) -> None:
        c = self.caminho(raiz, prd_id)
        c.parent.mkdir(parents=True, exist_ok=True)
        tmp = c.with_name(c.name + ".tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, c)

    def sessao(self, papel: str, provider: str, modelo: str | None) -> str | None:
        sid = self.sessoes.get(chave_sessao(papel, provider, modelo))
        if sid is None and provider == "anthropic":
            sid = self.sessoes.get(chave_sessao(papel, provider, ""))  # migrada da v1
        return sid or None

    def guardar_sessao(self, papel: str, provider: str, modelo: str | None, session_id: str | None) -> None:
        if session_id:
            self.sessoes[chave_sessao(papel, provider, modelo)] = session_id

    def limpar_tarefa(self) -> None:
        self.tarefa_ativa, self.rodada_atual, self.ultimo_retorno, self.historico_rodadas = None, 1, None, []
