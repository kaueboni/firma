"""Detecção do projeto-alvo e criação da pasta .firma/."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

PASTA = ".firma"
INBOX_INICIAL = "# INBOX da Firma\n\nPerguntas que a Firma não pôde decidir sozinha aparecem aqui.\n"


IGNORAR = {"node_modules", "dist", "build", "vendor", "target", "venv", "env", "__pycache__"}
MARCADORES = ("pyproject.toml", "package.json", "go.mod", "Cargo.toml", "Makefile", "setup.py")


@dataclass
class Parte:
    """Um pedaço do projeto com comandos próprios: a raiz (".") ou uma subpasta (ex.: "web")."""

    pasta: str
    testes: str | None = None
    lint: str | None = None


@dataclass
class Deteccao:
    raiz: Path
    nome: str
    branch_principal: str
    github: str | None = None  # "dono/repo"
    partes: list[Parte] = field(default_factory=list)
    tem_claude_md: bool = False
    avisos: list[str] = field(default_factory=list)


def _git(raiz: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=raiz, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def raiz_git(pasta: Path) -> Path | None:
    saida = _git(pasta, "rev-parse", "--show-toplevel")
    return Path(saida) if saida else None


def detectar_branch_principal(raiz: Path) -> str:
    ref = _git(raiz, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if ref:
        return ref.rsplit("/", 1)[-1]
    for nome in ("main", "master"):
        if _git(raiz, "rev-parse", "--verify", "--quiet", f"refs/heads/{nome}"):
            return nome
    return _git(raiz, "branch", "--show-current") or "main"


def parse_github(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _ler_toml(caminho: Path) -> dict:
    try:
        return tomllib.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _ler_json(caminho: Path) -> dict:
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _gerenciador_node(raiz: Path) -> str:
    if (raiz / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (raiz / "yarn.lock").exists():
        return "yarn"
    if (raiz / "bun.lockb").exists() or (raiz / "bun.lock").exists():
        return "bun"
    return "npm"


def _python(raiz: Path) -> tuple[str, str]:
    """Como chamar pytest e ruff nesta pasta: (prefixo para ferramenta, prefixo para módulo).

    Com uv.lock: `uv run`. Com um .venv: o Python do venv (Scripts no Windows, bin no resto),
    para não depender de o ambiente estar ativado. Senão, os comandos direto.
    """
    if (raiz / "uv.lock").exists():
        return "uv run ", "uv run "
    for venv in (".venv", "venv"):
        for sub in ("Scripts", "bin"):
            if (raiz / venv / sub).is_dir():
                py = f"{venv}/{sub}/python"
                return f"{py} -m ", f"{py} -m "
    return "", ""


def detectar_comandos(raiz: Path) -> tuple[str | None, str | None]:
    """Devolve (comando de testes, comando de lint) a partir dos arquivos de uma pasta."""
    testes = lint = None
    prefixo, _ = _python(raiz)

    pyproject = raiz / "pyproject.toml"
    if pyproject.exists():
        dados = _ler_toml(pyproject)
        texto = pyproject.read_text(encoding="utf-8", errors="ignore")
        if "pytest" in texto or (raiz / "tests").is_dir() or (raiz / "pytest.ini").exists():
            testes = f"{prefixo}pytest -q"
        if "ruff" in dados.get("tool", {}) or "ruff" in texto or (raiz / "ruff.toml").exists():
            lint = f"{prefixo}ruff check ."
    else:
        reqs = " ".join(
            p.read_text(encoding="utf-8", errors="ignore").lower() for p in raiz.glob("requirements*.txt")
        )
        marcadores_py = ("pytest.ini", "setup.py", "setup.cfg", "tox.ini")
        if "pytest" in reqs or any((raiz / m).exists() for m in marcadores_py):
            testes = f"{prefixo}pytest -q"
        elif reqs and (raiz / "tests").is_dir():
            testes = f"{prefixo}pytest -q"
        if "ruff" in reqs or (raiz / "ruff.toml").exists():
            lint = f"{prefixo}ruff check ."

    pacote = raiz / "package.json"
    if pacote.exists():
        scripts = _ler_json(pacote).get("scripts", {})
        pm = _gerenciador_node(raiz)
        if testes is None and "test" in scripts:
            testes = f"{pm} test"
        if lint is None and "lint" in scripts:
            lint = f"{pm} run lint"

    if testes is None and (raiz / "go.mod").exists():
        testes = "go test ./..."
    if testes is None and (raiz / "Cargo.toml").exists():
        testes = "cargo test"
    if testes is None and (raiz / "Makefile").exists():
        texto = (raiz / "Makefile").read_text(encoding="utf-8", errors="ignore")
        if re.search(r"^test\s*:", texto, re.MULTILINE):
            testes = "make test"
    return testes, lint


def detectar_partes(raiz: Path) -> list[Parte]:
    """A raiz e cada subpasta de primeiro nível que é um projeto (pyproject, package.json...)."""
    partes: list[Parte] = []
    candidatas = [raiz] + sorted(
        p for p in raiz.iterdir() if p.is_dir() and not p.name.startswith(".") and p.name not in IGNORAR
    )
    for pasta in candidatas:
        e_projeto = any((pasta / m).exists() for m in MARCADORES) or any(pasta.glob("requirements*.txt"))
        if not e_projeto:
            continue
        testes, lint = detectar_comandos(pasta)
        nome = "." if pasta == raiz else pasta.name
        if testes or lint or nome != ".":
            partes.append(Parte(nome, testes, lint))
    return partes


def detectar(pasta: Path) -> Deteccao | None:
    raiz = raiz_git(pasta)
    if raiz is None:
        return None
    d = Deteccao(
        raiz=raiz,
        nome=raiz.name,
        branch_principal=detectar_branch_principal(raiz),
        github=parse_github(_git(raiz, "remote", "get-url", "origin")),
        partes=detectar_partes(raiz),
        tem_claude_md=(raiz / "CLAUDE.md").exists() or (raiz / ".claude" / "CLAUDE.md").exists(),
    )
    if d.github is None:
        d.avisos.append(
            "Sem remoto do GitHub em `origin`: a Firma não vai conseguir abrir PRs. "
            "Para criar um: gh repo create --private --source . --remote origin --push"
        )
    if not any(p.testes for p in d.partes):
        d.avisos.append("Nenhum comando de testes encontrado: preencha `testes` no config.toml.")
    return d


def _toml_str(valor: str | None) -> str:
    if valor is None:
        return '""'
    return '"' + valor.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _partes_toml(partes: list[Parte]) -> str:
    if not partes:
        partes = [Parte(".")]
    blocos = [
        "\n# Uma [[partes]] por pedaço do projeto; os comandos rodam dentro de `pasta`.\n"
    ]
    for p in partes:
        blocos.append(
            "[[partes]]\n"
            f"pasta = {_toml_str(p.pasta)}\n"
            f"testes = {_toml_str(p.testes)}\n"
            f"lint = {_toml_str(p.lint)}\n"
        )
    return "\n".join(blocos)


def gerar_config(d: Deteccao) -> str:
    return (
        "# Configuração da Firma para este projeto. Gerado por `firma init`; pode editar.\n"
        "[projeto]\n"
        f"nome = {_toml_str(d.nome)}\n"
        f"branch_principal = {_toml_str(d.branch_principal)}\n"
        f"github = {_toml_str(d.github)}\n"
        f"{_partes_toml(d.partes)}"
        "\n[quota]\n"
        "# A Firma para quando a utilização semanal da conta chega a este teto (0 a 1).\n"
        "teto_semanal = 0.6\n"
        '# Janela em que pode rodar, "HH:MM-HH:MM". Vazio = qualquer hora.\n'
        'janela = ""\n'
    )


def garantir_gitignore(raiz: Path) -> bool:
    """Acrescenta `.firma/` ao .gitignore se ainda não estiver lá. Devolve True se mudou."""
    caminho = raiz / ".gitignore"
    atual = caminho.read_text(encoding="utf-8") if caminho.exists() else ""
    linhas = {l.strip() for l in atual.splitlines()}
    if {".firma", ".firma/", "/.firma", "/.firma/"} & linhas:
        return False
    separador = "" if atual == "" or atual.endswith("\n") else "\n"
    caminho.write_text(f"{atual}{separador}\n# Firma (local, fora do git)\n.firma/\n", encoding="utf-8")
    return True


def criar_pasta(d: Deteccao) -> Path:
    base = d.raiz / PASTA
    (base / "backlog").mkdir(parents=True, exist_ok=True)
    (base / "config.toml").write_text(gerar_config(d), encoding="utf-8")
    inbox = base / "INBOX.md"
    if not inbox.exists():
        inbox.write_text(INBOX_INICIAL, encoding="utf-8")
    return base
