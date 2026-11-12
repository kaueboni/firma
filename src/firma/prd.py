"""PRDs: criar a partir do modelo, ler, conferir a regra de pronto e mudar o status.

O PRD é o único ponto em que a pessoa decide (o advisor aprova a spec), então a
regra de pronto é o portão de entrada da fila.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from importlib import resources
from pathlib import Path

import yaml

TIPOS = ("feature", "bug", "refatoracao")
STATUS = ("rascunho", "pronto", "na-fila", "em-execucao", "pr-aberto", "estacionado")
TAMANHOS = ("P", "M", "G")

SECOES_BASE = ("Problema", "Critérios de aceite", "Fora de escopo", "Limites do advisor")
SECOES_TIPO = {
    "feature": ("Comportamento esperado",),
    "bug": ("Como reproduzir", "Atual vs. esperado", "Teste de regressão"),
    "refatoracao": ("O que não pode mudar", "Como medir"),
}
PLACEHOLDER = "[PREENCHER]"
PALAVRAS_VAGAS = (
    "melhor", "melhorar", "rápido", "rapido", "bonito", "intuitivo", "adequado", "adequada",
    "fácil", "facil", "amigável", "amigavel", "robusto", "eficiente", "bem feito", "corretamente",
)

_FRONTMATTER = re.compile(r"\A﻿?---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_ID = re.compile(r"^PRD-(\d{3,})$")


@dataclass
class Prd:
    caminho: Path
    meta: dict
    titulo: str
    secoes: dict[str, str]

    @property
    def id(self) -> str:
        return str(self.meta.get("id", ""))

    @property
    def tipo(self) -> str:
        return str(self.meta.get("tipo", ""))

    @property
    def status(self) -> str:
        return str(self.meta.get("status", ""))


@dataclass
class Resultado:
    erros: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)

    @property
    def pronto(self) -> bool:
        return not self.erros


# ---------------------------------------------------------------- leitura


def separar(texto: str) -> tuple[dict, str]:
    m = _FRONTMATTER.match(texto)
    if not m:
        raise ValueError("o PRD não começa com o cabeçalho entre linhas ---")
    meta = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta, dict):
        raise ValueError("o cabeçalho do PRD não é um mapa de campos")
    return meta, texto[m.end():]


def ler(caminho: Path) -> Prd:
    meta, corpo = separar(caminho.read_text(encoding="utf-8-sig"))
    titulo = ""
    secoes: dict[str, list[str]] = {}
    atual: str | None = None
    for linha in corpo.splitlines():
        if linha.startswith("# ") and not titulo:
            titulo = linha[2:].strip()
        elif linha.startswith("## "):
            atual = linha[3:].strip()
            secoes[atual] = []
        elif atual is not None:
            secoes[atual].append(linha)
    limpo = {nome: _sem_comentarios("\n".join(linhas)).strip() for nome, linhas in secoes.items()}
    return Prd(caminho=caminho, meta=meta, titulo=titulo, secoes=limpo)


def _sem_comentarios(texto: str) -> str:
    return re.sub(r"<!--.*?-->", "", texto, flags=re.DOTALL)


def itens(texto: str) -> list[str]:
    """Itens de lista (`- x`, `- [ ] x`, `1. x`), sem o marcador."""
    saida = []
    for linha in texto.splitlines():
        m = re.match(r"^\s*(?:[-*]|\d+\.)\s+(?:\[[ xX]\]\s+)?(.*\S)", linha)
        if m:
            saida.append(m.group(1))
    return saida


# ---------------------------------------------------------------- regra de pronto


def checar(prd: Prd, ids_existentes: set[str] | None = None) -> Resultado:
    r = Resultado()
    meta = prd.meta

    if not _ID.match(prd.id):
        r.erros.append(f"id inválido: {prd.id!r} (esperado PRD-001, PRD-002...)")
    if prd.tipo not in TIPOS:
        r.erros.append(f"tipo inválido: {prd.tipo!r} (use {', '.join(TIPOS)})")
    if prd.status not in STATUS:
        r.erros.append(f"status inválido: {prd.status!r}")
    tamanho = str(meta.get("tamanho", ""))
    if tamanho not in TAMANHOS:
        r.erros.append("tamanho ausente ou inválido (use P, M ou G)")
    elif tamanho == "G":
        r.erros.append("tamanho G não cabe numa execução: quebre em PRDs menores ligados por depende_de")
    prioridade = meta.get("prioridade")
    if not isinstance(prioridade, int) or not 1 <= prioridade <= 5:
        r.erros.append("prioridade deve ser um número de 1 a 5")
    if not prd.titulo or PLACEHOLDER in prd.titulo:
        r.erros.append("falta o título (linha que começa com # )")

    obrigatorias = list(SECOES_BASE) + list(SECOES_TIPO.get(prd.tipo, ()))
    for nome in obrigatorias:
        conteudo = prd.secoes.get(nome)
        if conteudo is None:
            r.erros.append(f"falta a seção \"{nome}\"")
        elif not conteudo:
            r.erros.append(f"a seção \"{nome}\" está vazia")
        elif PLACEHOLDER in conteudo:
            r.erros.append(f"a seção \"{nome}\" ainda tem {PLACEHOLDER}")

    criterios = itens(prd.secoes.get("Critérios de aceite", ""))
    if "Critérios de aceite" in prd.secoes and not criterios:
        r.erros.append("os critérios de aceite precisam ser uma lista (um item por critério)")
    for c in criterios:
        if PLACEHOLDER in c:
            continue
        vaga = any(re.search(rf"\b{re.escape(p)}\b", c, re.IGNORECASE) for p in PALAVRAS_VAGAS)
        concreto = bool(re.search(r"\d|`", c))
        if vaga and not concreto:
            r.avisos.append(f"critério parece vago, difícil de verificar: \"{c}\"")
    if len(criterios) > 8:
        r.avisos.append(f"{len(criterios)} critérios de aceite: talvez seja grande demais para uma execução")

    perguntas = [p for p in itens(prd.secoes.get("Perguntas em aberto", "")) if p.strip().lower() != "nenhuma"]
    if perguntas:
        r.erros.append(f"{len(perguntas)} pergunta(s) em aberto: responda antes de pôr na fila")

    deps = meta.get("depende_de") or []
    if not isinstance(deps, list):
        r.erros.append("depende_de deve ser uma lista, ex.: [PRD-001]")
    elif ids_existentes is not None:
        for dep in deps:
            if str(dep) not in ids_existentes:
                r.avisos.append(f"depende_de cita {dep}, que não está no backlog")
    return r


# ---------------------------------------------------------------- backlog


def slug(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", sem_acento.lower()).strip("-")
    return s[:50].rstrip("-") or "prd"


def listar(backlog: Path) -> list[Prd]:
    prds = []
    for caminho in sorted(backlog.glob("PRD-*.md")):
        try:
            prds.append(ler(caminho))
        except (ValueError, yaml.YAMLError, OSError):
            continue
    return prds


def encontrar(backlog: Path, alvo: str) -> Path | None:
    """Aceita o id (PRD-001), só o número (1, 001) ou um caminho."""
    caminho = Path(alvo)
    if caminho.suffix == ".md" and caminho.exists():
        return caminho
    numero = re.sub(r"\D", "", alvo)
    if not numero:
        return None
    prefixo = f"PRD-{int(numero):03d}"
    achados = sorted(backlog.glob(f"{prefixo}-*.md")) + sorted(backlog.glob(f"{prefixo}.md"))
    return achados[0] if achados else None


def proximo_id(backlog: Path) -> str:
    numeros = [int(m.group(1)) for p in backlog.glob("PRD-*.md") if (m := re.match(r"PRD-(\d+)", p.name))]
    return f"PRD-{max(numeros, default=0) + 1:03d}"


def renderizar(titulo: str, tipo: str, id_: str, hoje: date | None = None) -> str:
    if tipo not in TIPOS:
        raise ValueError(f"tipo inválido: {tipo}")
    modelo = resources.files("firma.modelos").joinpath("prd.md").read_text(encoding="utf-8")
    texto = (
        modelo.replace("{id}", id_)
        .replace("{tipo}", tipo)
        .replace("{titulo}", titulo)
        .replace("{data}", (hoje or date.today()).isoformat())
    )

    def manter(m: re.Match) -> str:
        return m.group(2) if m.group(1) == tipo else ""

    texto = re.sub(r"<!-- tipo: (\w+) -->\n(.*?)<!-- /tipo -->\n\n?", manter, texto, flags=re.DOTALL)
    return texto


def criar(backlog: Path, titulo: str, tipo: str) -> Path:
    backlog.mkdir(parents=True, exist_ok=True)
    id_ = proximo_id(backlog)
    caminho = backlog / f"{id_}-{slug(titulo)}.md"
    caminho.write_text(renderizar(titulo, tipo, id_), encoding="utf-8")
    return caminho


def mudar_status(caminho: Path, novo: str) -> None:
    if novo not in STATUS:
        raise ValueError(f"status inválido: {novo}")
    texto = caminho.read_text(encoding="utf-8-sig")
    m = _FRONTMATTER.match(texto)
    if not m:
        raise ValueError("o PRD não começa com o cabeçalho entre linhas ---")
    cabecalho, n = re.subn(r"(?m)^status:.*$", f"status: {novo}", m.group(1))
    if n == 0:
        cabecalho += f"\nstatus: {novo}"
    caminho.write_text(f"---\n{cabecalho}\n---\n{texto[m.end():]}", encoding="utf-8")
