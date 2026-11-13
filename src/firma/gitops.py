"""Git, testes e GitHub: tudo o que a Firma faz por conta própria, sem modelo."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Pastas que a worktree não tem (ignoradas ou não commitadas) e que vêm ligadas do repositório
# principal: ambientes de teste e as skills/instruções dos agentes (.opencode, .claude).
AMBIENTES = (".venv", "venv", "node_modules", ".opencode", ".claude")
# Instruções dos agentes que muitos projetos não commitam: copiadas para a worktree se faltarem.
INSTRUCOES = ("CLAUDE.md", "AGENTS.md")
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


def _e_ligacao(caminho: Path) -> bool:
    try:
        return caminho.is_symlink() or bool(getattr(caminho, "is_junction", lambda: False)())
    except OSError:
        return False


def ligar_ambientes(raiz: Path, wt: Path, partes: list[dict]) -> list[str]:
    """A worktree nasce sem .venv, node_modules e as skills locais (não estão no git). Em vez de
    reinstalar ou copiar, liga as pastas do repositório principal, para os testes e os agentes
    enxergarem o mesmo que no repositório."""
    ligados = []
    pastas = {"."} | {(p.get("pasta") or ".") for p in partes}
    for pasta in sorted(pastas):
        prefixo = "" if pasta == "." else f"{pasta}/"
        for nome in AMBIENTES:
            origem = raiz / pasta / nome
            destino = wt / pasta / nome
            if origem.is_dir() and not destino.exists() and destino.parent.exists():
                _ligar_pasta(origem, destino)
                ligados.append(prefixo + nome)
        # .claude commitado (ex.: settings.json) mas skills fora do git: liga só as skills.
        origem, destino = raiz / pasta / ".claude" / "skills", wt / pasta / ".claude" / "skills"
        if origem.is_dir() and not destino.exists() and destino.parent.is_dir() and not _e_ligacao(destino.parent):
            _ligar_pasta(origem, destino)
            ligados.append(prefixo + ".claude/skills")
        for nome in INSTRUCOES:
            origem, destino = raiz / pasta / nome, wt / pasta / nome
            if origem.is_file() and not destino.exists() and destino.parent.exists():
                shutil.copyfile(origem, destino)
                ligados.append(prefixo + nome)
    return ligados


def _pathspec(wt: Path) -> list[str]:
    """SEM_AMBIENTES mais as instruções copiadas para a worktree (as que não estão no git)."""
    extras = []
    for nome in INSTRUCOES:
        if (wt / nome).exists() and not git(wt, "ls-files", "--", nome, checar=False):
            extras.append(f":(exclude){nome}")
    return SEM_AMBIENTES + extras


def _comando_para_o_sistema(comando: str) -> str:
    """No Windows, o cmd.exe não aceita `.venv/Scripts/python`: troca as barras do executável."""
    if os.name != "nt":
        return comando
    partes = comando.split(" ", 1)
    if "/" in partes[0] and not partes[0].startswith(("-", '"')):
        partes[0] = partes[0].replace("/", "\\")
    return " ".join(partes)


def tem_mudancas(wt: Path) -> bool:
    return bool(git(wt, "status", "--porcelain", *_pathspec(wt)))


def tem_mudancas_codigo(wt: Path, docs: str) -> bool:
    """Mudanças fora de docs/firma/<PRD>/: a anotação do advisor em decisoes.md não conta como código."""
    return bool(git(wt, "status", "--porcelain", *_pathspec(wt), f":(exclude,glob){docs}/**"))


def arquivos_alterados(wt: Path) -> list[str]:
    """Caminhos alterados ou novos (relativos à worktree, com /), sem os ambientes."""
    r = _rodar(["git", "-c", "core.quotepath=false", "status", "--porcelain", "-uall", *_pathspec(wt)], wt)
    if r.returncode != 0:
        raise ErroGit(f"git status: {(r.stderr or r.stdout).strip()}")
    caminhos = []
    for linha in r.stdout.splitlines():  # sem strip: a 1ª coluna pode ser espaço
        if len(linha) < 4:
            continue
        caminho = linha[3:].strip()
        if " -> " in caminho:
            caminho = caminho.split(" -> ", 1)[1]
        caminhos.append(caminho.strip('"'))
    return caminhos


def status_curto(wt: Path) -> str:
    return git(wt, "-c", "core.quotepath=false", "status", "--porcelain", *_pathspec(wt))


def sha_atual(wt: Path) -> str:
    return git(wt, "rev-parse", "--short", "HEAD", checar=False)


def _remover(wt: Path, caminhos: list[str]) -> None:
    rastreados = set(git(wt, "-c", "core.quotepath=false", "ls-files", checar=False).splitlines())
    for caminho in caminhos:
        if caminho in rastreados:
            git(wt, "checkout", "--", caminho, checar=False)
            continue
        alvo = wt / caminho
        if _e_ligacao(alvo):
            continue
        if alvo.is_dir():
            shutil.rmtree(alvo, ignore_errors=True)
        else:
            alvo.unlink(missing_ok=True)


def foto(wt: Path) -> dict[str, bytes | None]:
    """Retrato das alterações pendentes (conteúdo de cada arquivo alterado), para desfazer depois
    só o que uma sessão sem permissão de escrita mexeu, sem perder o trabalho do developer."""
    retrato: dict[str, bytes | None] = {}
    for caminho in arquivos_alterados(wt):
        alvo = wt / caminho
        retrato[caminho] = alvo.read_bytes() if alvo.is_file() else None
    return retrato


def restaurar(wt: Path, retrato: dict[str, bytes | None], permitido: str | None = None) -> list[str]:
    """Volta a worktree ao `retrato`, exceto dentro de `permitido`. Devolve o que desfez."""
    prefixo = permitido.rstrip("/") + "/" if permitido else None
    desfeitos = []
    atuais = set(arquivos_alterados(wt))
    for caminho in sorted(atuais | set(retrato)):
        if prefixo and caminho.startswith(prefixo):
            continue
        alvo = wt / caminho
        atual = alvo.read_bytes() if alvo.is_file() else None
        if caminho in retrato:
            if atual != retrato[caminho]:
                if retrato[caminho] is None:
                    alvo.unlink(missing_ok=True)  # estava apagado no retrato
                else:
                    alvo.parent.mkdir(parents=True, exist_ok=True)
                    alvo.write_bytes(retrato[caminho])
                desfeitos.append(caminho)
        else:
            _remover(wt, [caminho])
            desfeitos.append(caminho)
    return desfeitos


def reverter_fora_de(wt: Path, pasta: str) -> list[str]:
    """Desfaz o que mudou fora de `pasta` (o tech-lead só escreve a spec). Devolve o que desfez."""
    prefixo = pasta.rstrip("/") + "/"
    fora = [c for c in arquivos_alterados(wt) if not c.startswith(prefixo)]
    _remover(wt, fora)
    return fora


def descartar(wt: Path) -> list[str]:
    """Desfaz mudanças não commitadas (quando um papel sem escrita mexeu em algo).
    Não usa `git clean` na worktree inteira: os ambientes ligados e as instruções copiadas ficam."""
    alterados = arquivos_alterados(wt)
    _remover(wt, alterados)
    return alterados


def diff(wt: Path, limite: int = 60_000) -> str:
    git(wt, "add", "-A", *_pathspec(wt))
    texto = git(wt, "diff", "--cached", "--stat") + "\n\n" + git(wt, "diff", "--cached")
    git(wt, "reset", "-q")
    return texto if len(texto) <= limite else texto[:limite] + "\n[diff cortado]"


def commit(wt: Path, mensagem: str) -> str | None:
    git(wt, "add", "-A", *_pathspec(wt))
    if not git(wt, "diff", "--cached", "--name-only"):
        return None
    git(wt, "commit", "-q", "-m", mensagem)
    return git(wt, "rev-parse", "--short", "HEAD")


@dataclass
class ResultadoComando:
    pasta: str
    tipo: str  # "testes" ou "lint"
    comando: str
    ok: bool
    saida: str


def _rodar_esperando(comando: str, cwd: Path, timeout: int, enquanto: Callable[[], None] | None) -> tuple[int, str]:
    """Roda no shell e, enquanto espera, chama `enquanto` (a renovação da trava do run)."""
    with tempfile.TemporaryFile() as saida:
        proc = subprocess.Popen(comando, cwd=cwd, shell=True, stdout=saida, stderr=subprocess.STDOUT)
        inicio = time.monotonic()
        while True:
            try:
                proc.wait(timeout=5)
                break
            except subprocess.TimeoutExpired:
                if enquanto:
                    enquanto()
                if time.monotonic() - inicio > timeout:
                    proc.kill()
                    proc.wait()
                    raise
        saida.seek(0)
        return proc.returncode, saida.read().decode("utf-8", errors="replace")


def rodar_verificacoes(wt: Path, partes: list[dict], enquanto: Callable[[], None] | None = None) -> list[ResultadoComando]:
    resultados = []
    for parte in partes:
        pasta = parte.get("pasta") or "."
        for tipo in ("testes", "lint"):
            comando = (parte.get(tipo) or "").strip()
            if not comando:
                continue
            try:
                codigo, saida = _rodar_esperando(_comando_para_o_sistema(comando), wt / pasta, 1200, enquanto)
                resultados.append(ResultadoComando(pasta, tipo, comando, codigo == 0, saida.strip()[-6000:]))
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
