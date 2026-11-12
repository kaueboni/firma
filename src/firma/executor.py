"""O turno da Firma: pega PRDs prontos e leva cada um até um PR em rascunho.

As regras do processo (ordem, rodadas, paradas, quota, commits, push) são código aqui.
Os modelos só fazem o que precisa de inteligência: spec, código, auditoria e conselho.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from firma import gitops, prd as prdmod
from firma.registro import home_firma
from firma.sessao import Pedido, Quota, Resposta

RodarSessao = Callable[[Pedido, Quota], Awaitable[Resposta]]

LEITURA = ["Read", "Grep", "Glob"]
TAREFA = re.compile(r"^\s*-\s*\[( |x|X)\]\s*(T\d+)\s*[:.)\-–—]\s*(.+?)\s*$", re.MULTILINE)
VEREDITO = re.compile(r"VEREDITO:\s*\**\s*(APROVADO|REPROVADO)", re.IGNORECASE)


# ---------------------------------------------------------------- estado em disco


def arquivo_parar() -> Path:
    return home_firma() / "parar"


def arquivo_trava() -> Path:
    return home_firma() / "run.lock"


def pedir_parada() -> None:
    arquivo_parar().parent.mkdir(parents=True, exist_ok=True)
    arquivo_parar().write_text(datetime.now().isoformat(), encoding="utf-8")


def trava_ativa(validade_s: int = 30 * 60) -> dict | None:
    """A trava vale enquanto o run a renova (a cada evento). Velha demais = run morreu."""
    caminho = arquivo_trava()
    if not caminho.exists():
        return None
    if time.time() - caminho.stat().st_mtime > validade_s:
        return None
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def salvar_quota(q: Quota) -> None:
    caminho = home_firma() / "quota.json"
    caminho.parent.mkdir(parents=True, exist_ok=True)
    dados = {**q.__dict__, "em": datetime.now().isoformat(timespec="seconds")}
    caminho.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")


def ler_quota() -> dict:
    try:
        return json.loads((home_firma() / "quota.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


@dataclass
class EstadoPrd:
    fase: str = "spec"  # spec → tarefas → fim
    feitas: list[str] = field(default_factory=list)
    sessoes: dict[str, str] = field(default_factory=dict)
    pr: str = ""
    decisoes: list[str] = field(default_factory=list)

    @staticmethod
    def caminho(raiz: Path, prd_id: str) -> Path:
        return raiz / ".firma" / "estado" / f"{prd_id}.json"

    @classmethod
    def carregar(cls, raiz: Path, prd_id: str) -> "EstadoPrd":
        c = cls.caminho(raiz, prd_id)
        if c.exists():
            return cls(**json.loads(c.read_text(encoding="utf-8")))
        return cls()

    def salvar(self, raiz: Path, prd_id: str) -> None:
        c = self.caminho(raiz, prd_id)
        c.parent.mkdir(parents=True, exist_ok=True)
        c.write_text(json.dumps(self.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")


def tarefas(plano: str) -> list[tuple[str, str, bool]]:
    return [(m.group(2).upper(), m.group(3), m.group(1).lower() == "x") for m in TAREFA.finditer(plano)]


def veredito(texto: str) -> bool:
    achados = VEREDITO.findall(texto or "")
    return bool(achados) and achados[-1].upper() == "APROVADO"


def dentro_da_janela(janela: str, agora: datetime | None = None) -> bool:
    if not janela:
        return True
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$", janela)
    if not m:
        return True
    h1, m1, h2, m2 = map(int, m.groups())
    agora = agora or datetime.now()
    atual, ini, fim = agora.hour * 60 + agora.minute, h1 * 60 + m1, h2 * 60 + m2
    return ini <= atual < fim if ini <= fim else atual >= ini or atual < fim


# ---------------------------------------------------------------- prompts


def _prompt_techlead(prd_texto: str, docs: str, partes: list[dict]) -> str:
    return f"""Você é o tech-lead da Firma. Leia o PRD abaixo e o código do repositório, e escreva dois arquivos:

1. `{docs}/spec.md`: a spec técnica. Como a feature vai ser feita neste código: arquivos que mudam, contratos (entradas e saídas de funções, APIs, eventos), riscos e o que os testes precisam cobrir. Curta e concreta.
2. `{docs}/plano.md`: as tarefas, na ordem, cada uma pequena o bastante para um developer fazer e testar sozinho. Use EXATAMENTE este formato, uma linha por tarefa, seguida de 1 a 3 linhas explicando:

- [ ] T1: título curto
  O que fazer e qual teste prova que ficou pronto.
- [ ] T2: ...

Regras: não escreva código nem testes; só esses dois arquivos. Cada critério de aceite do PRD precisa estar coberto por alguma tarefa. Comandos de verificação do projeto: {json.dumps(partes, ensure_ascii=False)}.

PRD:
{prd_texto}"""


def _prompt_revisao_spec(prd_texto: str, docs: str) -> str:
    return f"""Você é o advisor da Firma e decide em nome do dono do projeto. Revise `{docs}/spec.md` e `{docs}/plano.md` contra o PRD abaixo e o código.

Aprove se a spec é viável neste código, cobre todos os critérios de aceite e respeita o "Fora de escopo" e os "Limites do advisor". Se reprovar, liste só os problemas que impedem a implementação, cada um com a correção.

Termine com uma linha exatamente assim: VEREDITO: APROVADO ou VEREDITO: REPROVADO

PRD:
{prd_texto}"""


def _prompt_developer(tarefa: tuple[str, str, bool], docs: str, partes: list[dict], retorno: str | None) -> str:
    tid, titulo, _ = tarefa
    base = f"""Você é o developer da Firma. Implemente SOMENTE a tarefa {tid} ("{titulo}") de `{docs}/plano.md`, seguindo `{docs}/spec.md` e o PRD em `{docs}/prd.md`.

Use TDD: escreva ou ajuste o teste primeiro, veja falhar, implemente, veja passar. Rode os testes e o lint das partes que você mexer: {json.dumps(partes, ensure_ascii=False)}.

Não faça commit, push nem mude de branch (a Firma faz isso). Não instale dependência nova. Não mexa em tarefas futuras. Termine com um resumo de 3 a 5 linhas do que mudou."""
    if retorno:
        base += f"\n\nA rodada anterior desta tarefa foi recusada. Corrija exatamente isto:\n{retorno}"
    return base


def _prompt_auditor(tarefa: tuple[str, str, bool], docs: str, diff: str, verificacoes: str) -> str:
    tid, titulo, _ = tarefa
    return f"""Você é o auditor da Firma. Audite a tarefa {tid} ("{titulo}") de `{docs}/plano.md`, contra `{docs}/spec.md` e `{docs}/prd.md`. Você não edita arquivos.

Confira: a tarefa foi feita por inteiro e só ela; há teste que prova o comportamento; nada fora do escopo; nada que viole os "Limites do advisor" do PRD (por exemplo, dependência nova); código legível e coerente com o resto do projeto.

Verificações que a Firma já rodou:
{verificacoes}

Mudanças da tarefa:
{diff}

Se reprovar, liste só os itens críticos ou importantes, cada um com a correção esperada. Termine com uma linha exatamente assim: VEREDITO: APROVADO ou VEREDITO: REPROVADO"""


def _prompt_diagnostico(tarefa: tuple[str, str, bool], docs: str, historico: list[str]) -> str:
    tid, titulo, _ = tarefa
    pareceres = "\n\n---\n\n".join(historico)
    return f"""Você é o advisor da Firma. A tarefa {tid} ("{titulo}") de `{docs}/plano.md` foi recusada em todas as rodadas. Leia os pareceres abaixo e o código atual na worktree, e diga em até 10 linhas: a causa raiz e a correção que o developer deve fazer. Se a tarefa for impossível sem algo que você não pode decidir (mudança irreversível, dependência nova, algo fora do PRD), comece a resposta com "BLOQUEIO:" e explique.

Pareceres:
{pareceres}"""


def _texto_verificacoes(resultados: list[gitops.ResultadoComando]) -> str:
    if not resultados:
        return "(o projeto não tem comandos de testes ou lint configurados)"
    linhas = []
    for r in resultados:
        linhas.append(f"[{r.pasta}] {r.tipo}: `{r.comando}` → {'ok' if r.ok else 'FALHOU'}")
        if not r.ok:
            linhas.append(r.saida[-3000:])
    return "\n".join(linhas)


# ---------------------------------------------------------------- execução de um PRD


class Pausa(Exception):
    """Para o turno sem estacionar a feature (quota, janela, pedido de parada)."""


class Estacionar(Exception):
    """A feature não pode seguir sem a pessoa: vai para o INBOX."""


@dataclass
class Turno:
    raiz: Path
    cfg: dict
    registro: Callable
    quota: Quota
    rodar_sessao: RodarSessao
    sem_pr: bool = False

    @property
    def partes(self) -> list[dict]:
        return self.cfg.get("partes") or []

    def checar_limites(self) -> None:
        if arquivo_parar().exists():
            raise Pausa("parada pedida (firma parar)")
        if not dentro_da_janela(self.cfg["quota"].get("janela", "")):
            raise Pausa("fora da janela configurada")
        teto = float(self.cfg["quota"].get("teto_semanal", 0.6))
        if self.quota.semanal is not None and self.quota.semanal >= teto:
            raise Pausa(f"quota semanal em {self.quota.semanal:.0%}, teto de {teto:.0%}")
        if self.quota.rejeitado:
            raise Pausa("limite da assinatura atingido")
        arquivo_trava().touch(exist_ok=True)

    async def sessao(self, pedido: Pedido) -> Resposta:
        self.checar_limites()
        pedido.permissao = self.cfg["execucao"].get("permissao", "bypassPermissions")
        if not pedido.modelo and self.cfg["modelos"].get("trabalho"):
            pedido.modelo = self.cfg["modelos"]["trabalho"]
        r = await self.rodar_sessao(pedido, self.quota)
        salvar_quota(self.quota)
        if r.sem_quota:
            raise Pausa("limite da assinatura atingido durante a sessão")
        if r.erro:
            raise Estacionar(f"{pedido.papel}: {r.erro}")
        return r

    def _corpo_pr(self, p: prdmod.Prd, estado: EstadoPrd, lista: list[tuple[str, str, bool]], nota: str = "") -> str:
        itens = "\n".join(f"- [{'x' if t in estado.feitas else ' '}] {t}: {titulo}" for t, titulo, _ in lista)
        decisoes = "\n".join(f"- {d}" for d in estado.decisoes) or "- nenhuma"
        corpo = (
            f"PR aberto pela **Firma** a partir do {p.id}. Spec, plano e PRD em `docs/firma/{p.id}/`.\n\n"
            f"## Tarefas\n{itens}\n\n## Decisões do advisor\n{decisoes}\n"
        )
        if nota:
            corpo += f"\n## Situação\n{nota}\n"
        return corpo

    async def executar(self, caminho_prd: Path) -> str:
        p = prdmod.ler(caminho_prd)
        estado = EstadoPrd.carregar(self.raiz, p.id)
        base = self.cfg["projeto"].get("branch_principal") or "main"
        github = (self.cfg["projeto"].get("github") or "").strip()
        usar_pr = bool(github) and not self.sem_pr
        branch = f"firma/{p.id}"
        docs = f"docs/firma/{p.id}"
        log = lambda texto, papel="firma": self.registro(texto, prd=p.id, papel=papel)  # noqa: E731

        prdmod.mudar_status(caminho_prd, "em-execucao")
        wt = gitops.preparar_worktree(self.raiz, p.id, base)
        log(f"worktree {wt} na branch {branch}")
        ligados = gitops.ligar_ambientes(self.raiz, wt, self.partes)
        if ligados:
            log("ambientes ligados do repositório principal: " + ", ".join(ligados))
        (wt / docs).mkdir(parents=True, exist_ok=True)
        (wt / docs / "prd.md").write_text(caminho_prd.read_text(encoding="utf-8-sig"), encoding="utf-8")
        prd_texto = (wt / docs / "prd.md").read_text(encoding="utf-8")
        lista: list[tuple[str, str, bool]] = []

        try:
            if estado.fase == "spec":
                r = await self.sessao(Pedido(
                    "tech-lead", _prompt_techlead(prd_texto, docs, self.partes), wt, p.id,
                    resume=estado.sessoes.get("tech-lead"), escrita_so_em=docs,
                    ferramentas=LEITURA + ["Write", "Edit"], advisor=self.cfg["modelos"].get("advisor") or None,
                ))
                estado.sessoes["tech-lead"] = r.session_id or ""
                lista = tarefas(_ler(wt / docs / "plano.md"))
                if not lista:
                    raise Estacionar("o tech-lead não produziu um plano no formato `- [ ] T1: título`")
                log(f"spec e plano: {len(lista)} tarefa(s)", "tech-lead")

                rev = await self.sessao(Pedido(
                    "advisor", _prompt_revisao_spec(prd_texto, docs), wt, p.id, pode_escrever=False,
                    ferramentas=LEITURA, modelo=self.cfg["modelos"].get("advisor") or None,
                ))
                if not veredito(rev.texto):
                    log("reprovou a spec; o tech-lead corrige uma vez", "advisor")
                    r = await self.sessao(Pedido(
                        "tech-lead", f"O advisor reprovou a spec. Corrija spec.md e plano.md:\n\n{rev.texto}",
                        wt, p.id, resume=estado.sessoes.get("tech-lead"), escrita_so_em=docs,
                        ferramentas=LEITURA + ["Write", "Edit"],
                    ))
                    lista = tarefas(_ler(wt / docs / "plano.md"))
                    if not lista:
                        raise Estacionar("o plano corrigido ficou sem tarefas no formato esperado")
                    estado.decisoes.append("spec revisada uma vez a pedido do advisor")
                estado.decisoes.append("spec e plano aprovados pelo advisor")
                _anotar(wt / docs / "decisoes.md", "Revisão da spec", rev.texto)
                gitops.commit(wt, f"docs({p.id}): spec e plano\n\nGerado pela Firma.")
                estado.fase = "tarefas"
                if usar_pr:
                    gitops.push(wt, branch)
                    estado.pr = gitops.criar_pr(wt, base, branch, f"{p.id}: {p.titulo}", self._corpo_pr(p, estado, lista))
                    log(f"PR em rascunho: {estado.pr}")
                estado.salvar(self.raiz, p.id)

            lista = tarefas(_ler(wt / docs / "plano.md"))
            for tarefa in lista:
                tid, titulo, _ = tarefa
                if tid in estado.feitas:
                    continue
                log(f"{tid}: {titulo}")
                await self._fazer_tarefa(p, wt, docs, tarefa, estado)
                _marcar_feita(wt / docs / "plano.md", tid)
                sha = gitops.commit(wt, f"feat({p.id}): {tid} — {titulo}\n\nGerado pela Firma.")
                estado.feitas.append(tid)
                estado.salvar(self.raiz, p.id)
                log(f"commit {sha or '(sem mudanças)'} · quota semana {_pct(self.quota.semanal)}")
                if usar_pr:
                    gitops.push(wt, branch)
                    gitops.atualizar_pr(wt, estado.pr, self._corpo_pr(p, estado, lista))

            estado.fase = "fim"
            estado.salvar(self.raiz, p.id)
            if usar_pr:
                gitops.atualizar_pr(wt, estado.pr, self._corpo_pr(p, estado, lista, "Todas as tarefas aprovadas. Pronto para a sua revisão."))
                prdmod.mudar_status(caminho_prd, "pr-aberto")
                log(f"pronto para revisão: {estado.pr}")
                return "pr-aberto"
            prdmod.mudar_status(caminho_prd, "estacionado")
            _inbox(self.raiz, p.id, f"Todas as tarefas prontas na branch `{branch}`, mas sem GitHub configurado não abri PR.")
            return "estacionado"

        except Pausa as e:
            estado.salvar(self.raiz, p.id)
            prdmod.mudar_status(caminho_prd, "pronto")  # volta para a fila e retoma de onde parou
            log(f"pausado: {e}")
            raise
        except (Estacionar, gitops.ErroGit) as e:
            estado.salvar(self.raiz, p.id)
            prdmod.mudar_status(caminho_prd, "estacionado")
            _inbox(self.raiz, p.id, str(e))
            if usar_pr and estado.pr:
                gitops.atualizar_pr(wt, estado.pr, self._corpo_pr(p, estado, lista, f"Estacionado: {e}"))
            log(f"estacionado: {e}")
            return "estacionado"

    async def _fazer_tarefa(self, p, wt: Path, docs: str, tarefa, estado: EstadoPrd) -> None:
        rodadas = int(self.cfg["execucao"].get("rodadas", 3))
        retorno: str | None = None
        historico: list[str] = []
        advisor = self.cfg["modelos"].get("advisor") or None

        for rodada in range(1, rodadas + 2):  # a última só acontece depois do diagnóstico do advisor
            if rodada == rodadas + 1:
                diag = await self.sessao(Pedido(
                    "advisor", _prompt_diagnostico(tarefa, docs, historico), wt, p.id,
                    pode_escrever=False, ferramentas=LEITURA, modelo=advisor,
                ))
                _anotar(wt / docs / "decisoes.md", f"Diagnóstico da {tarefa[0]}", diag.texto)
                if diag.texto.strip().upper().startswith("BLOQUEIO"):
                    raise Estacionar(f"{tarefa[0]}: {diag.texto.strip()[:600]}")
                estado.decisoes.append(f"{tarefa[0]}: última rodada com o diagnóstico do advisor")
                retorno = f"Diagnóstico do advisor (última chance):\n{diag.texto}"

            dev = await self.sessao(Pedido(
                "developer", _prompt_developer(tarefa, docs, self.partes, retorno), wt, p.id,
                resume=estado.sessoes.get("developer"), advisor=advisor,
            ))
            estado.sessoes["developer"] = dev.session_id or estado.sessoes.get("developer", "")
            estado.salvar(self.raiz, p.id)

            if not gitops.tem_mudancas(wt):
                retorno = "Nenhum arquivo foi alterado. Implemente a tarefa."
                historico.append(f"Rodada {rodada}: o developer não alterou nada.")
                continue

            verificacoes = gitops.rodar_verificacoes(wt, self.partes)
            texto_verif = _texto_verificacoes(verificacoes)
            if any(not v.ok for v in verificacoes):
                self.registro(f"verificações falharam (rodada {rodada})", prd=p.id, papel="firma")
                retorno = f"As verificações falharam:\n{texto_verif}"
                historico.append(f"Rodada {rodada}: verificações falharam.\n{texto_verif[-1500:]}")
                continue

            aud = await self.sessao(Pedido(
                "auditor", _prompt_auditor(tarefa, docs, gitops.diff(wt), texto_verif), wt, p.id,
                resume=estado.sessoes.get("auditor"), pode_escrever=False, ferramentas=LEITURA + ["Bash"],
            ))
            estado.sessoes["auditor"] = aud.session_id or estado.sessoes.get("auditor", "")
            if veredito(aud.texto):
                self.registro(f"{tarefa[0]} aprovada", prd=p.id, papel="auditor")
                return
            self.registro(f"{tarefa[0]} reprovada (rodada {rodada})", prd=p.id, papel="auditor")
            retorno = aud.texto
            historico.append(f"Rodada {rodada}: auditor reprovou.\n{aud.texto[-2000:]}")

        raise Estacionar(f"{tarefa[0]} reprovada em todas as rodadas, inclusive depois do diagnóstico do advisor")


# ---------------------------------------------------------------- auxiliares


def _ler(caminho: Path) -> str:
    return caminho.read_text(encoding="utf-8") if caminho.exists() else ""


def _pct(valor: float | None) -> str:
    return "?" if valor is None else f"{valor:.0%}"


def _marcar_feita(plano: Path, tid: str) -> None:
    texto = _ler(plano)
    novo = re.sub(rf"^(\s*-\s*)\[ \](\s*{tid}\b)", r"\1[x]\2", texto, count=1, flags=re.MULTILINE)
    plano.write_text(novo, encoding="utf-8")


def _anotar(arquivo: Path, titulo: str, texto: str) -> None:
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    with arquivo.open("a", encoding="utf-8") as f:
        f.write(f"## {titulo} ({datetime.now():%d/%m %H:%M})\n\n{texto.strip()}\n\n")


def _inbox(raiz: Path, prd_id: str, motivo: str) -> None:
    caminho = raiz / ".firma" / "INBOX.md"
    with caminho.open("a", encoding="utf-8") as f:
        f.write(f"\n## {prd_id} — {datetime.now():%d/%m %H:%M}\n\n{motivo}\n")


def dependencias_ok(raiz: Path, p: prdmod.Prd, backlog: Path) -> tuple[bool, str]:
    for dep in p.meta.get("depende_de") or []:
        caminho = prdmod.encontrar(backlog, str(dep))
        if caminho is None:
            return False, f"depende de {dep}, que não está no backlog"
        if not gitops.pr_mergeado(raiz, f"firma/{dep}"):
            return False, f"esperando o merge do PR de {dep}"
    return True, ""


def fila(raiz: Path) -> list[Path]:
    """PRDs prontos (inclui os pausados, que voltam como pronto), por prioridade e id."""
    backlog = raiz / ".firma" / "backlog"
    prds = [p for p in prdmod.listar(backlog) if p.status == "pronto"]
    prds.sort(key=lambda p: (int(p.meta.get("prioridade", 3)), p.id))
    return [p.caminho for p in prds]


# ---------------------------------------------------------------- o turno inteiro


async def rodar_turno(
    rodar_sessao: RodarSessao,
    registro: Callable,
    projeto: str | None = None,
    max_prds: int | None = None,
    sem_pr: bool = False,
) -> dict:
    """Passa por todos os projetos registrados (ou um só) até a fila acabar ou um limite parar."""
    import os

    from firma import config, registro as reg

    if trava_ativa():
        raise RuntimeError("já existe um `firma run` rodando nesta máquina (veja `firma status`)")
    arquivo_trava().parent.mkdir(parents=True, exist_ok=True)
    arquivo_trava().write_text(json.dumps({"pid": os.getpid(), "inicio": datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")
    arquivo_parar().unlink(missing_ok=True)

    quota = Quota()
    resumo: dict = {"pr-aberto": [], "estacionado": [], "pausa": "", "pulados": []}
    feitos = 0
    try:
        for proj in reg.listar():
            if projeto and proj["nome"] != projeto:
                continue
            raiz = Path(proj["caminho"])
            if not (raiz / ".firma" / "config.toml").exists():
                registro(f"projeto {proj['nome']} sem .firma/config.toml, pulado")
                continue
            cfg = config.carregar(raiz)
            turno = Turno(raiz, cfg, registro, quota, rodar_sessao, sem_pr)
            backlog = raiz / ".firma" / "backlog"
            for caminho in fila(raiz):
                if max_prds is not None and feitos >= max_prds:
                    return resumo
                p = prdmod.ler(caminho)
                ok, motivo = dependencias_ok(raiz, p, backlog)
                if not ok:
                    registro(f"pulado: {motivo}", prd=p.id)
                    resumo["pulados"].append(p.id)
                    continue
                turno.checar_limites()
                situacao = await turno.executar(caminho)
                resumo.setdefault(situacao, []).append(f"{proj['nome']}/{p.id}")
                feitos += 1
        if feitos == 0:
            registro("nada na fila: nenhum PRD com status pronto")
        return resumo
    except Pausa as e:
        resumo["pausa"] = str(e)
        return resumo
    finally:
        arquivo_trava().unlink(missing_ok=True)
        arquivo_parar().unlink(missing_ok=True)
        registro(
            f"fim do turno: {len(resumo['pr-aberto'])} PR(s), {len(resumo['estacionado'])} estacionado(s)"
            + (f", parou por: {resumo['pausa']}" if resumo["pausa"] else "")
            + f" · quota semana {_pct(quota.semanal)}"
        )
