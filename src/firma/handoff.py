"""Handoff determinístico: o que o próximo agente precisa saber ao pegar uma tarefa no meio.

Usado quando um papel troca de provedor (quota do Claude → Gemini) ou quando uma tarefa
interrompida é retomada. Montado pela Firma a partir do git e do estado, nunca pelo modelo.
Salvo em .firma/estado/<PRD>-handoff.md, fora da worktree (não suja o diff nem entra em commit).
"""

from __future__ import annotations

import re
from pathlib import Path

from firma import gitops
from firma.estado import EstadoPrd

LIMITE_DIFF = 12_000
_SEGREDO = re.compile(r"(?im)^([+-].*\b(api[_-]?key|secret|token|password|senha)\b\s*[:=]).*$")


def _sem_segredos(texto: str) -> str:
    return _SEGREDO.sub(r"\1 [omitido pela Firma]", texto)


def montar(
    prd_id: str,
    branch: str,
    wt: Path,
    estado: EstadoPrd,
    tarefa: tuple[str, str] | None,
    motivo: str,
) -> str:
    status = gitops.status_curto(wt) or "(nenhuma alteração pendente)"
    diff = gitops.diff(wt, limite=LIMITE_DIFF) if status != "(nenhuma alteração pendente)" else ""
    linhas = [
        "## Handoff da Firma",
        "",
        f"Motivo: {motivo}.",
        f"PRD: {prd_id} · branch `{branch}` · worktree `{wt}` · último commit {gitops.sha_atual(wt) or '?'}",
        f"Tarefas concluídas: {', '.join(estado.feitas) or 'nenhuma'}",
    ]
    if tarefa:
        linhas.append(f"Tarefa ativa: {tarefa[0]} — {tarefa[1]} (rodada {estado.rodada_atual})")
    linhas += ["", "Arquivos alterados ou novos na worktree (git status --porcelain):", "```", status, "```"]
    if diff:
        linhas += [
            "",
            "A worktree já contém alterações parciais não commitadas da tentativa anterior; "
            "inspecione os arquivos atuais antes de editar. Resumo do diff:",
            "```diff",
            _sem_segredos(diff),
            "```",
        ]
    if estado.ultimo_retorno:
        linhas += ["", "Último retorno (testes/lint ou parecer):", estado.ultimo_retorno[-4000:]]
    return "\n".join(linhas) + "\n"


def salvar(raiz: Path, prd_id: str, texto: str) -> Path:
    caminho = raiz / ".firma" / "estado" / f"{prd_id}-handoff.md"
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(texto, encoding="utf-8")
    return caminho
