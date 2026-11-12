"""Git, testes e GitHub: tudo o que a Firma faz por conta própria, sem modelo."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

AMBIENTES = (".venv", "venv", "node_modules")
# Os ambientes ligados nunca entram em commit, mesmo num projeto que não os ignora no .gitignore.
SEM_AMBIENTES = ["--", "."] + [f":(exclude,glob)**/{nome}" for nome in AMBIENTES] + [
    f":(exclude,glob)**/{nome}/**" for nome in AMBIENTES
]


class ErroGit(RuntimeError):
    pass


def _rodar(cmd: list[str] | str, cwd: Path, timeout: int = 600, shell: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, shell=shell
    )


def git(cwd: Path, *args: str, checar: bool = True) -> str:
    r = _rodar(["git", *args], cwd)
    if checar and r.returncode != 0:
        raise ErroGit(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def preparar_worktree(raiz: Path, prd_id: str, base: str) -> Path:
    """Cria (ou reaproveita) a worktree .firma/worktrees/<PRD> na branch firma/<PRD>."""
    destino = raiz / ".firma" / "worktrees" / prd_id
    branch = f"firma/{prd_id}"
    if (destino / ".git").exists():
        return destino
    destino.parent.mkdir(parents=True, exist_ok=True)
    git(raiz, "fetch", "origin", base, checar=False)
    origem = f"origin/{base}" if git(raiz, "rev-parse", "--verify", "--quiet", f"origin/{base}", checar=False) else base
    existe = git(raiz, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", checar=False)
    if existe:
        git(raiz, "worktree", "add", str(destino), branch)
    else:
        git(raiz, "worktree", "add", "-b", branch, str(destino), origem)
    return destino


def _ligar_pasta(origem: Path, destino: Path) -> None:
    """Liga uma pasta à outra sem copiar: junção no Windows (não pede admin), symlink no resto."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(origem), str(destino))
    else:
        destino.symlink_to(origem, target_is_directory=True)


def ligar_ambientes(raiz: Path, wt: Path, partes: list[dict]) -> list[str]:
    """A worktree nasce sem .venv e node_modules (são ignorados pelo git). Em vez de reinstalar,
    liga as pastas do repositório principal, para os testes e o lint rodarem igual."""
    ligados = []
    pastas = {"."} | {(p.get("pasta") or ".") for p in partes}
    for pasta in sorted(pastas):
        for nome in AMBIENTES:
            origem = raiz / pasta / nome
            destino = wt / pasta / nome
            if origem.is_dir() and not destino.exists() and destino.parent.exists():
                _ligar_pasta(origem, destino)
                ligados.append(f"{pasta}/{nome}" if pasta != "." else nome)
    return ligados


def _comando_para_o_sistema(comando: str) -> str:
    """No Windows, o cmd.exe não aceita `.venv/Scripts/python`: troca as barras do executável."""
    if os.name != "nt":
        return comando
    partes = comando.split(" ", 1)
    if "/" in partes[0] and not partes[0].startswith(("-", '"')):
        partes[0] = partes[0].replace("/", "\\")
    return " ".join(partes)


def tem_mudancas(wt: Path) -> bool:
    return bool(git(wt, "status", "--porcelain", *SEM_AMBIENTES))


def diff(wt: Path, limite: int = 60_000) -> str:
    git(wt, "add", "-A", *SEM_AMBIENTES)
    texto = git(wt, "diff", "--cached", "--stat") + "\n\n" + git(wt, "diff", "--cached")
    git(wt, "reset", "-q")
    return texto if len(texto) <= limite else texto[:limite] + "\n[diff cortado]"


def commit(wt: Path, mensagem: str) -> str | None:
    git(wt, "add", "-A", *SEM_AMBIENTES)
    if not git(wt, "diff", "--cached", "--name-only"):
        return None
    git(wt, "commit", "-q", "-m", mensagem)
    return git(wt, "rev-parse", "--short", "HEAD")


def descartar(wt: Path) -> None:
    """Desfaz mudanças não commitadas (usado quando um papel sem escrita mexeu em algo)."""
    git(wt, "checkout", "--", ".", checar=False)
    git(wt, "clean", "-fdq", checar=False)


@dataclass
class ResultadoComando:
    pasta: str
    tipo: str  # "testes" ou "lint"
    comando: str
    ok: bool
    saida: str


def rodar_verificacoes(wt: Path, partes: list[dict]) -> list[ResultadoComando]:
    resultados = []
    for parte in partes:
        pasta = parte.get("pasta") or "."
        for tipo in ("testes", "lint"):
            comando = (parte.get(tipo) or "").strip()
            if not comando:
                continue
            try:
                r = _rodar(_comando_para_o_sistema(comando), wt / pasta, timeout=1200, shell=True)
                saida = (r.stdout + "\n" + r.stderr).strip()
                resultados.append(ResultadoComando(pasta, tipo, comando, r.returncode == 0, saida[-6000:]))
            except subprocess.TimeoutExpired:
                resultados.append(ResultadoComando(pasta, tipo, comando, False, "tempo esgotado (20 min)"))
    return resultados


# ---------------------------------------------------------------- GitHub (gh)


def push(wt: Path, branch: str) -> None:
    git(wt, "push", "-q", "-u", "origin", branch)


def criar_pr(wt: Path, base: str, branch: str, titulo: str, corpo: str) -> str:
    r = _rodar(["gh", "pr", "create", "--draft", "--base", base, "--head", branch, "--title", titulo, "--body", corpo], wt)
    if r.returncode != 0:
        raise ErroGit(f"gh pr create: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip().splitlines()[-1]


def atualizar_pr(wt: Path, url: str, corpo: str) -> None:
    _rodar(["gh", "pr", "edit", url, "--body", corpo], wt)


def pr_mergeado(raiz: Path, branch: str) -> bool:
    r = _rodar(["gh", "pr", "view", branch, "--json", "state", "--jq", ".state"], raiz)
    return r.returncode == 0 and r.stdout.strip() == "MERGED"
