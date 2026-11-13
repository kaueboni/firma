"""Linha de comando da Firma."""

from __future__ import annotations

import sys

import anyio
import typer

from firma import __version__

# No Windows, quando a saída vai para um pipe (como no shell do Claude Code), o Python usa cp1252
# e quebra ao imprimir "✓". Forçamos UTF-8 e trocamos o que não couber em vez de falhar.
for _fluxo in (sys.stdout, sys.stderr):
    try:
        _fluxo.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
from firma.doctor import AVISO, FALHA, OK, rodar_checagens

app = typer.Typer(help="Firma: PRDs entram, PRs saem.", no_args_is_help=True, add_completion=False)

_SIMBOLO = {OK: "✓", AVISO: "!", FALHA: "✗"}


def _versao(valor: bool) -> None:
    if valor:
        typer.echo(f"firma {__version__}")
        raise typer.Exit()


@app.callback()
def principal(
    versao: bool = typer.Option(False, "--version", callback=_versao, is_eager=True, help="Mostra a versão."),
) -> None:
    """Firma: um time de agentes que transforma PRDs em PRs."""


@app.command()
def doctor(
    quota: bool = typer.Option(
        False, "--quota", help="Faz uma chamada mínima pelo Agent SDK e mostra o estado da quota."
    ),
    opencode: bool = typer.Option(
        False, "--opencode", help="Sobe o OpenCode e faz uma chamada mínima em cada modelo não-Claude do projeto."
    ),
) -> None:
    """Confere se esta máquina tem o que a Firma precisa."""
    checagens = rodar_checagens()
    for c in checagens:
        typer.echo(f" {_SIMBOLO[c.estado]} {c.nome}: {c.detalhe}")

    falhou = any(c.estado == FALHA for c in checagens)
    if quota:
        if falhou:
            typer.echo("\nCorrija as falhas acima antes de sondar a quota.")
            raise typer.Exit(1)
        _sondar_quota()
    if opencode and not _sondar_opencode():
        raise typer.Exit(1)
    if falhou:
        raise typer.Exit(1)


def _sondar_opencode() -> bool:
    from pathlib import Path

    from firma import config, projeto
    from firma.runtime import opencode as oc

    raiz = projeto.raiz_git(Path.cwd()) or Path.cwd()
    modelos: list[tuple[str, str]] = []
    if (raiz / ".firma" / "config.toml").exists():
        cfg = config.carregar(raiz)
        for papel in config.SECOES_PAPEL:
            m = config.modelo_do_papel(cfg, papel)
            for prov, mod in ((m.provider, m.model), (m.fallback_provider, m.fallback_model)):
                if prov and prov != "anthropic" and (prov, mod) not in modelos:
                    modelos.append((prov, mod))
    if not modelos:
        modelos = [config.FALLBACK_PADRAO]
    typer.echo("")
    typer.echo(f"Sondando o OpenCode em {raiz} ({', '.join(f'{p}/{m}' for p, m in modelos)})...")
    resultado = anyio.run(oc.sondar, raiz, modelos)
    typer.echo(f"Detalhe salvo em {oc.salvar_sonda(resultado)}")
    if resultado.get("erro"):
        typer.echo(f" ✗ OpenCode: {resultado['erro']}")
        return False
    typer.echo(f" ✓ OpenCode {resultado.get('versao')} · provedores conectados: {', '.join(resultado.get('conectados') or []) or 'nenhum'}")
    tudo_ok = True
    for item in resultado["modelos"]:
        if item["ok"]:
            typer.echo(f" ✓ {item['provider']}/{item['model']}: respondeu ({item['tokens']} tokens)")
        else:
            tudo_ok = False
            typer.echo(f" ✗ {item['provider']}/{item['model']}: {item['erro']}")
            if item["provider"] not in (resultado.get("conectados") or []):
                typer.echo(f"   Conecte com `opencode auth login` e escolha {item['provider']} (para o Gemini, chave do AI Studio).")
            elif item.get("tipo_erro") == "configuracao":
                typer.echo(f"   Troque o modelo em .firma/config.toml (`opencode models {item['provider']}` lista os disponíveis).")
    return tudo_ok


@app.command()
def init(
    sim: bool = typer.Option(False, "--sim", "-y", help="Não pede confirmação."),
    forcar: bool = typer.Option(False, "--forcar", help="Reescreve um config.toml que já existe."),
) -> None:
    """Prepara o projeto atual para a Firma: detecta, mostra e cria a pasta .firma/."""
    from pathlib import Path

    from firma import projeto, registro

    d = projeto.detectar(Path.cwd())
    if d is None:
        typer.echo("Esta pasta não está dentro de um repositório git.")
        raise typer.Exit(1)

    config = d.raiz / projeto.PASTA / "config.toml"
    if config.exists() and not forcar:
        typer.echo(f"A Firma já está configurada aqui ({config}). Use --forcar para reescrever.")
        raise typer.Exit(1)

    typer.echo(f"Projeto: {d.nome}  ({d.raiz})")
    typer.echo(f"  branch principal: {d.branch_principal}")
    typer.echo(f"  GitHub:           {d.github or '—'}")
    typer.echo(f"  CLAUDE.md:        {'sim' if d.tem_claude_md else 'não'}   AGENTS.md: {'sim' if d.tem_agents_md else 'não'}")
    typer.echo(f"  skills:           .claude/skills {d.skills_claude} · .opencode/skills {d.skills_opencode}")
    if not d.partes:
        typer.echo("  partes:           nenhuma reconhecida")
    for p in d.partes:
        rotulo = "raiz" if p.pasta == "." else f"{p.pasta}/"
        typer.echo(f"  [{rotulo}] testes: {p.testes or '—'}   lint: {p.lint or '—'}")
    for aviso in d.avisos:
        typer.echo(f" ! {aviso}")
    typer.echo("\nVai criar .firma/ (config.toml, backlog/, INBOX.md), pôr .firma/ no .gitignore e registrar o projeto.")
    typer.echo("Nada no seu código, no CLAUDE.md ou em .claude/ é alterado.")

    if not sim and not typer.confirm("Continuar?", default=True):
        raise typer.Exit(1)

    base = projeto.criar_pasta(d)
    mudou_gitignore = projeto.garantir_gitignore(d.raiz)
    novo = registro.registrar(d.raiz, d.nome)
    typer.echo(f"\n ✓ {base} criada")
    if mudou_gitignore:
        typer.echo(" ✓ .firma/ acrescentada ao .gitignore (lembre de commitar essa linha)")
    typer.echo(" ✓ projeto registrado" if novo else " ✓ projeto já estava registrado")
    typer.echo(f"\nRevise {base / 'config.toml'} se algum comando estiver errado.")
    typer.echo("Próximo passo: `firma claude instalar` (uma vez por máquina) e `/prd <ideia>` no Claude Code.")


@app.command()
def projetos() -> None:
    """Lista os projetos registrados nesta máquina."""
    from firma import registro

    lista = registro.listar()
    if not lista:
        typer.echo("Nenhum projeto registrado. Rode `firma init` dentro de um repositório.")
        return
    for p in lista:
        typer.echo(f" • {p['nome']}  {p['caminho']}")


def _sondar_quota() -> None:
    from firma.quota import resumir, salvar, sondar

    typer.echo("\nSondando a quota (uma chamada mínima pelo Agent SDK)...")
    sonda = anyio.run(sondar)
    linhas, veredito = resumir(sonda)
    for linha in linhas:
        typer.echo(f"  {linha}")
    typer.echo("")
    for linha in veredito:
        typer.echo(f" → {linha}")
    typer.echo(f"\nResposta completa salva em {salvar(sonda)}")


# ---------------------------------------------------------------- firma prd

prd_app = typer.Typer(help="PRDs: criar, conferir e pôr na fila.", no_args_is_help=True)
app.add_typer(prd_app, name="prd")


def _backlog():
    from pathlib import Path

    from firma import projeto

    raiz = projeto.raiz_git(Path.cwd())
    base = raiz / projeto.PASTA if raiz else None
    if base is None or not (base / "config.toml").exists():
        typer.echo("A Firma não está configurada neste projeto. Rode `firma init` na raiz do repositório.")
        raise typer.Exit(1)
    return base / "backlog"


def _abrir(alvo: str):
    from firma import prd

    backlog = _backlog()
    caminho = prd.encontrar(backlog, alvo)
    if caminho is None:
        typer.echo(f"PRD não encontrado: {alvo}")
        raise typer.Exit(1)
    try:
        return backlog, caminho, prd.ler(caminho)
    except ValueError as e:
        typer.echo(f"{caminho.name}: {e}")
        raise typer.Exit(1)


def _mostrar_resultado(r) -> None:
    for e in r.erros:
        typer.echo(f" ✗ {e}")
    for a in r.avisos:
        typer.echo(f" ! {a}")


@prd_app.command("novo")
def prd_novo(
    titulo: str = typer.Argument(..., help="Título curto do PRD."),
    tipo: str = typer.Option("feature", "--tipo", "-t", help="feature, bug ou refatoracao."),
) -> None:
    """Cria um PRD no backlog a partir do modelo do tipo escolhido."""
    from firma import prd

    if tipo not in prd.TIPOS:
        typer.echo(f"Tipo inválido: {tipo}. Use {', '.join(prd.TIPOS)}.")
        raise typer.Exit(1)
    caminho = prd.criar(_backlog(), titulo, tipo)
    typer.echo(f"Criado: {caminho}")
    typer.echo("Preencha as seções com [PREENCHER] (ou use /prd no Claude Code) e rode `firma prd checar`.")


@prd_app.command("listar")
def prd_listar() -> None:
    """Lista os PRDs do backlog com tipo, status e tamanho."""
    from firma import prd

    prds = prd.listar(_backlog())
    if not prds:
        typer.echo("Backlog vazio. Crie um PRD com `firma prd novo` ou `/prd` no Claude Code.")
        return
    for p in prds:
        typer.echo(f" {p.id:8} {p.status:12} {p.tipo:12} {str(p.meta.get('tamanho', '?')):2} {p.titulo}")


@prd_app.command("checar")
def prd_checar(alvo: str = typer.Argument(..., help="Id (PRD-001 ou 1) ou caminho do arquivo.")) -> None:
    """Confere a regra de pronto sem mudar nada."""
    from firma import prd

    backlog, caminho, p = _abrir(alvo)
    r = prd.checar(p, {x.id for x in prd.listar(backlog)})
    _mostrar_resultado(r)
    if r.pronto:
        typer.echo(f" ✓ {p.id} passa na regra de pronto. Para pôr na fila: firma prd pronto {p.id}")
    else:
        raise typer.Exit(1)


@prd_app.command("pronto")
def prd_pronto(alvo: str = typer.Argument(..., help="Id (PRD-001 ou 1) ou caminho do arquivo.")) -> None:
    """Confere a regra de pronto e, se passar, muda o status para `pronto` (entra na fila)."""
    from firma import prd

    backlog, caminho, p = _abrir(alvo)
    r = prd.checar(p, {x.id for x in prd.listar(backlog)})
    _mostrar_resultado(r)
    if not r.pronto:
        typer.echo(f"\n{p.id} continua em {p.status}: corrija os erros acima.")
        raise typer.Exit(1)
    if p.status in ("em-execucao", "estacionado"):
        from firma import executor

        if executor.trava_ativa() is not None:
            typer.echo(f"{p.id} está em {p.status} e há um turno rodando; espere ou use `firma parar`.")
            raise typer.Exit(1)
        typer.echo(f" ✓ {p.id} estava em {p.status}; volta para a fila e retoma de onde parou.")
    elif p.status not in ("rascunho", "pronto"):
        typer.echo(f"{p.id} está em {p.status}; não volta para a fila.")
        raise typer.Exit(1)
    prd.mudar_status(caminho, "pronto")
    typer.echo(f" ✓ {p.id} está pronto e entra na fila.")


# ---------------------------------------------------------------- firma claude

claude_app = typer.Typer(help="Integração com o Claude Code.", no_args_is_help=True)
app.add_typer(claude_app, name="claude")


@claude_app.command("instalar")
def claude_instalar(
    forcar: bool = typer.Option(False, "--forcar", help="Sobrescreve comandos que você editou."),
) -> None:
    """Instala os comandos da Firma (/prd) no Claude Code, para todos os projetos."""
    from firma import claude

    for nome, situacao in claude.instalar(forcar):
        if situacao == "editado":
            typer.echo(f" ! /{nome}: você editou este comando; use --forcar para substituir")
        else:
            typer.echo(f" ✓ /{nome}: {situacao}")
    typer.echo(f"Pasta: {claude.pasta_comandos()}")


# ---------------------------------------------------------------- turno: run, status, parar, assistir


@app.command()
def run(
    projeto: str = typer.Option(None, "--projeto", "-p", help="Só este projeto (nome em `firma projetos`)."),
    max_prds: int = typer.Option(None, "--max", help="Para depois de N PRDs."),
    sem_pr: bool = typer.Option(False, "--sem-pr", help="Não faz push nem abre PR (fica só na branch local)."),
    detalhe: bool = typer.Option(False, "--detalhe", help="Mostra também o texto dos agentes."),
) -> None:
    """Pega os PRDs prontos e leva cada um até um PR em rascunho."""
    from firma import executor
    from firma.eventos import Registro
    from firma.runtime.roteador import Roteador

    registro = Registro(mostrar=typer.echo)

    async def principal_async():
        async with Roteador(registro, detalhe) as sessao:
            return await executor.rodar_turno(sessao, registro, projeto, max_prds, sem_pr)

    try:
        resumo = anyio.run(principal_async)
    except KeyboardInterrupt:
        typer.echo("\nInterrompido. O PRD em andamento volta para a fila e retoma de onde parou.")
        raise typer.Exit(130)
    except Exception as e:
        import traceback

        registro(f"erro: {type(e).__name__}: {e}", traceback=traceback.format_exc())
        typer.echo(f"\nErro: {type(e).__name__}: {e or '(sem mensagem)'}")
        typer.echo(traceback.format_exc())
        raise typer.Exit(1)
    if resumo.get("erro"):
        typer.echo(f"\nErro no turno: {resumo['erro']} (detalhe em `firma status` e em ~/.firma/logs)")
    if resumo.get("pr-aberto"):
        typer.echo("\nPRs para revisar: " + ", ".join(resumo["pr-aberto"]))
    if resumo.get("estacionado"):
        typer.echo("Estacionados (veja .firma/INBOX.md): " + ", ".join(resumo["estacionado"]))


@app.command()
def status() -> None:
    """Mostra se há um turno rodando, a quota e a fila de cada projeto."""
    from firma import executor, prd, registro
    from firma.eventos import arquivo_do_dia, formatar
    import json as _json
    from pathlib import Path

    trava = executor.trava_ativa()
    typer.echo(f"Turno: {'rodando desde ' + str(trava.get('inicio', '?')) if trava is not None else 'parado'}")
    q = executor.ler_quota()
    if q:
        typer.echo(f"Quota (às {q.get('em', '?')[11:16]}): semana {executor._pct(q.get('semanal'))}, 5 horas {executor._pct(q.get('cinco_horas'))}")
    for proj in registro.listar():
        backlog = Path(proj["caminho"]) / ".firma" / "backlog"
        prds = prd.listar(backlog) if backlog.exists() else []
        contagem = {}
        for p in prds:
            contagem[p.status] = contagem.get(p.status, 0) + 1
        texto = ", ".join(f"{n} {s}" for s, n in contagem.items()) or "backlog vazio"
        typer.echo(f" • {proj['nome']}: {texto}")
    log = arquivo_do_dia()
    if log.exists():
        typer.echo("\nÚltimos eventos:")
        for linha in log.read_text(encoding="utf-8").splitlines()[-8:]:
            typer.echo("  " + formatar(_json.loads(linha)))


@app.command()
def parar() -> None:
    """Pede para o turno parar na próxima fronteira de tarefa (depois do commit)."""
    from firma import executor

    if executor.trava_ativa() is None:
        typer.echo("Nenhum turno rodando.")
        return
    executor.pedir_parada()
    typer.echo("Pedido registrado: o turno para depois da sessão atual, sem perder trabalho.")


@app.command()
def assistir() -> None:
    """Acompanha ao vivo o que a Firma está fazendo (Ctrl+C para sair)."""
    from firma.eventos import seguir

    try:
        for linha in seguir():
            typer.echo(linha)
    except KeyboardInterrupt:
        pass
