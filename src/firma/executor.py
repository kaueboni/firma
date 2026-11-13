"""O turno da Firma: pega PRDs prontos e leva cada um até um PR em rascunho.

As regras do processo (ordem, rodadas, paradas, quota, fallback, commits, push) são código aqui.
Os modelos só fazem o que precisa de inteligência: spec, código, auditoria e conselho.
"""

from __future__ import annotations

import json
import re
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from firma import gitops, handoff as handoffmod, prd as prdmod
from firma.config import modelo_do_papel, SECOES_PAPEL
from firma.estado import EstadoCorrompido, EstadoPrd
from firma.registro import home_firma
from firma.runtime import erros
from firma.runtime.base import Pedido, Quota, Resposta
from firma.trava import (  # noqa: F401  (reexportados para a CLI e os testes)
    arquivo_parar,
    arquivo_trava,
    criar_trava,
    pedir_parada,
    renovar_trava,
    soltar_trava,
    trava_ativa,
)

RodarSessao = Callable[[Pedido, Quota], Awaitable[Resposta]]

LEITURA = ["Read", "Grep", "Glob", "Skill"]
ESCRITA = ["Write", "Edit"]
TAREFA = re.compile(r"^\s*-\s*\[( |x|X)\]\s*(T\d+)\s*[:.)\-–—]\s*(.+?)\s*$", re.MULTILINE)
VEREDITO = re.compile(r"\bVEREDITO\**\s*:\s*\**\s*(APROVADO|REPROVADO)\b", re.IGNORECASE)
SKILLS = (
    "Antes de começar, veja as skills do projeto (ferramenta Skill) e as instruções em CLAUDE.md/AGENTS.md, "
    "se existirem, e siga-as."
)


# ---------------------------------------------------------------- quota em disco


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

{SKILLS}

Regras: não escreva código nem testes; só esses dois arquivos, e só dentro de `{docs}/`. Cada critério de aceite do PRD precisa estar coberto por alguma tarefa. Comandos de verificação do projeto: {json.dumps(partes, ensure_ascii=False)}.

PRD:
{prd_texto}"""


def _prompt_correcao_spec(docs: str, parecer: str) -> str:
    return f"""O advisor reprovou a spec. Corrija `{docs}/spec.md` e `{docs}/plano.md` (mantendo o formato `- [ ] T1: título` no plano) para resolver exatamente estes pontos:

{parecer}"""


def _prompt_revisao_spec(prd_texto: str, docs: str) -> str:
    return f"""Você é o advisor da Firma e decide em nome do dono do projeto. Revise `{docs}/spec.md` e `{docs}/plano.md` contra o PRD abaixo e o código. Você só lê: não edita arquivos nem roda comandos.

{SKILLS}

Aprove se a spec é viável neste código, cobre todos os critérios de aceite e respeita o "Fora de escopo" e os "Limites do advisor". Se reprovar, liste só os problemas que impedem a implementação, cada um com a correção.

Termine com uma linha exatamente assim: VEREDITO: APROVADO ou VEREDITO: REPROVADO

PRD:
{prd_texto}"""


def _prompt_developer(tarefa: tuple[str, str, bool], docs: str, partes: list[dict], retorno: str | None) -> str:
    tid, titulo, _ = tarefa
    base = f"""Você é o developer da Firma. Implemente SOMENTE a tarefa {tid} ("{titulo}") de `{docs}/plano.md`, seguindo `{docs}/spec.md` e o PRD em `{docs}/prd.md`.

{SKILLS}

Use TDD: escreva ou ajuste o teste primeiro, veja falhar, implemente, veja passar. Rode os testes e o lint das partes que você mexer: {json.dumps(partes, ensure_ascii=False)}.

Não faça commit, push nem mude de branch (a Firma faz isso). Não instale dependência nova. Não mexa em tarefas futuras, em `{docs}/`, em `.claude/` nem em `.opencode/`. Termine com um resumo de 3 a 5 linhas do que mudou."""
    if retorno:
        base += f"\n\nA rodada anterior desta tarefa foi recusada. Corrija exatamente isto:\n{retorno}"
    return base


def _prompt_auditor(tarefa: tuple[str, str, bool], docs: str, diff: str, verificacoes: str) -> str:
    tid, titulo, _ = tarefa
    return f"""Você é o auditor da Firma. Audite a tarefa {tid} ("{titulo}") de `{docs}/plano.md`, contra `{docs}/spec.md` e `{docs}/prd.md`. Você só lê: não edita arquivos nem roda comandos (a Firma já rodou os testes e o lint).

{SKILLS}

Confira: a tarefa foi feita por inteiro e só ela; há teste que prova o comportamento; nada fora do escopo; nada que viole os "Limites do advisor" do PRD (por exemplo, dependência nova); código legível e coerente com o resto do projeto.

Verificações que a Firma já rodou:
{verificacoes}

Mudanças da tarefa:
{diff}

Se reprovar, liste só os itens críticos ou importantes, cada um com a correção esperada. Termine com uma linha exatamente assim: VEREDITO: APROVADO ou VEREDITO: REPROVADO"""


def _prompt_diagnostico(tarefa: tuple[str, str, bool], docs: str, historico: list[str]) -> str:
    tid, titulo, _ = tarefa
    pareceres = "\n\n---\n\n".join(historico)
    return f"""Você é o advisor da Firma. A tarefa {tid} ("{titulo}") de `{docs}/plano.md` foi recusada em todas as rodadas. Leia os pareceres abaixo e o código atual na worktree (só leitura), e diga em até 10 linhas: a causa raiz e a correção que o developer deve fazer. Se a tarefa for impossível sem algo que você não pode decidir (mudança irreversível, dependência nova, algo fora do PRD), comece a resposta com "BLOQUEIO:" e explique.

{SKILLS}

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
class _Contexto:
    """O PRD em execução, para as sessões montarem o handoff quando trocam de provedor."""

    prd_id: str
    branch: str
    wt: Path
    docs: str
    estado: EstadoPrd
    tarefa: tuple[str, str] | None = None


@dataclass
class Turno:
    raiz: Path
    cfg: dict
    registro: Callable
    quota: Quota
    rodar_sessao: RodarSessao
    sem_pr: bool = False
    # provedores sem quota neste `firma run` (compartilhado entre projetos)
    esgotados: set[str] = field(default_factory=set)
    _ctx: _Contexto | None = None

    @property
    def partes(self) -> list[dict]:
        return self.cfg.get("partes") or []

    def _teto_atingido(self) -> str | None:
        teto = float(self.cfg["quota"].get("teto_semanal", 0.6))
        if self.quota.rejeitado:
            return "limite da assinatura Claude atingido"
        if self.quota.semanal is not None and self.quota.semanal >= teto:
            return f"quota semanal em {self.quota.semanal:.0%}, teto de {teto:.0%}"
        return None

    def _sem_saida(self) -> list[str]:
        """Papéis que não têm nenhum provedor disponível agora."""
        presos = []
        for papel in SECOES_PAPEL:
            m = modelo_do_papel(self.cfg, papel)
            opcoes = [m.provider] + ([m.fallback_provider] if m.tem_fallback else [])
            if all(p in self.esgotados for p in opcoes):
                presos.append(papel)
        return presos

    def checar_limites(self) -> None:
        if arquivo_parar().exists():
            raise Pausa("parada pedida (firma parar)")
        if not dentro_da_janela(self.cfg["quota"].get("janela", "")):
            raise Pausa("fora da janela configurada")
        motivo = self._teto_atingido()
        if motivo and "anthropic" not in self.esgotados:
            self.esgotados.add("anthropic")
            self.registro(f"{motivo}: o Claude fica de fora pelo resto do turno")
        if motivo and self._sem_saida():
            raise Pausa(motivo)
        if not arquivo_trava().exists():
            criar_trava()
        renovar_trava(forcar=True)

    def _alvos(self, papel: str) -> list[tuple[str, str]]:
        m = modelo_do_papel(self.cfg, papel)
        alvos = [(m.provider, m.model)]
        if m.tem_fallback and (m.fallback_provider, m.fallback_model) != alvos[0]:
            alvos.append((m.fallback_provider, m.fallback_model))
        return alvos

    def _handoff(self, motivo: str) -> str:
        c = self._ctx
        if c is None:
            return ""
        texto = handoffmod.montar(c.prd_id, c.branch, c.wt, c.estado, c.tarefa, motivo)
        handoffmod.salvar(self.raiz, c.prd_id, texto)
        return texto

    async def sessao(
        self,
        papel: str,
        prompt: str,
        wt: Path,
        prd_id: str,
        estado: EstadoPrd,
        retomar: bool = False,
        com_handoff: str | None = None,
        **kw,
    ) -> Resposta:
        """Roda o papel no provedor configurado; sem quota, passa para o fallback com handoff."""
        self.checar_limites()
        alvos = self._alvos(papel)
        trocou_de: str | None = None
        for provider, modelo in alvos:
            if provider in self.esgotados:
                trocou_de = trocou_de or "anthropic"
                continue
            texto = prompt
            if trocou_de:
                texto += "\n\n" + self._handoff(f"o provedor {trocou_de} ficou sem quota; você continua o trabalho do papel {papel}")
            elif com_handoff:
                texto += "\n\n" + self._handoff(com_handoff)
            pedido = Pedido(
                papel, texto, wt, prd_id,
                resume=estado.sessao(papel, provider, modelo) if retomar and not trocou_de else None,
                provider=provider, modelo=modelo or None,
                permissao=self.cfg["execucao"].get("permissao", "bypassPermissions"),
                **kw,
            )
            r = await self.rodar_sessao(pedido, self.quota)
            salvar_quota(self.quota)
            if r.sem_quota or r.tipo_erro == erros.QUOTA:
                self.esgotados.add(provider)
                estado.provedores_esgotados = sorted(self.esgotados)
                proximos = [a for a in alvos if a[0] not in self.esgotados]
                if proximos:
                    self.registro(
                        f"limite do provedor {provider} atingido em {papel}; alternando para {proximos[0][0]}/{proximos[0][1]}",
                        prd=prd_id, papel="firma",
                    )
                trocou_de = provider
                continue
            if r.erro:
                causa = f"{papel} ({provider}/{modelo or 'padrão'}): {r.erro}"
                if r.tipo_erro and r.tipo_erro != erros.DESCONHECIDO:
                    causa += f" [{r.tipo_erro}]"
                raise Estacionar(causa)
            estado.guardar_sessao(papel, provider, modelo, r.session_id)
            return r
        raise Pausa(f"limite de quota atingido ({papel} sem provedor disponível)")

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

    def _so_leitura(self, wt: Path, retrato: dict, papel: str, prd_id: str, permitido: str | None = None) -> None:
        """Desfaz o que um papel sem escrita (ou com escrita restrita) mexeu fora do permitido."""
        desfeitos = gitops.restaurar(wt, retrato, permitido)
        if desfeitos:
            self.registro(f"{papel} alterou o que não podia; desfeito: {', '.join(desfeitos[:10])}", prd=prd_id, papel="firma")

    async def _techlead(self, p, wt, docs, estado, prompt) -> list[tuple[str, str, bool]]:
        retrato = gitops.foto(wt)
        await self.sessao(
            "tech-lead", prompt, wt, p.id, estado, retomar=True,
            escrita_so_em=docs, ferramentas=LEITURA + ESCRITA,
        )
        self._so_leitura(wt, retrato, "tech-lead", p.id, permitido=docs)
        return tarefas(_ler(wt / docs / "plano.md"))

    async def _advisor(self, p, wt, prompt, estado) -> Resposta:
        retrato = gitops.foto(wt)
        r = await self.sessao("advisor", prompt, wt, p.id, estado, pode_escrever=False, ferramentas=LEITURA)
        self._so_leitura(wt, retrato, "advisor", p.id)
        return r

    async def _fase_spec(self, p, wt, docs, estado, prd_texto, log) -> list[tuple[str, str, bool]]:
        lista = tarefas(_ler(wt / docs / "plano.md"))
        if lista and (wt / docs / "spec.md").exists():
            log("reaproveitando spec e plano da tentativa anterior", "tech-lead")
        else:
            lista = await self._techlead(p, wt, docs, estado, _prompt_techlead(prd_texto, docs, self.partes))
        if not lista:
            raise Estacionar("o tech-lead não produziu um plano no formato `- [ ] T1: título`")
        log(f"spec e plano: {len(lista)} tarefa(s)", "tech-lead")
        estado.salvar(self.raiz, p.id)

        rev = await self._advisor(p, wt, _prompt_revisao_spec(prd_texto, docs), estado)
        if not veredito(rev.texto):
            log("reprovou a spec; o tech-lead corrige uma vez", "advisor")
            _anotar(wt / docs / "decisoes.md", "Revisão da spec (reprovada)", rev.texto)
            lista = await self._techlead(p, wt, docs, estado, _prompt_correcao_spec(docs, rev.texto))
            if not lista:
                raise Estacionar("o plano corrigido ficou sem tarefas no formato `- [ ] T1: título`")
            estado.decisoes.append("spec revisada uma vez a pedido do advisor")
            estado.salvar(self.raiz, p.id)
            rev = await self._advisor(p, wt, _prompt_revisao_spec(prd_texto, docs), estado)
            if not veredito(rev.texto):
                _anotar(wt / docs / "decisoes.md", "Segunda revisão da spec (reprovada)", rev.texto)
                raise Estacionar(
                    "O advisor reprovou a spec mesmo após a correção do tech-lead.\n\nParecer:\n" + rev.texto.strip()[:2000]
                )
        estado.decisoes.append("spec e plano aprovados pelo advisor")
        _anotar(wt / docs / "decisoes.md", "Revisão da spec (aprovada)", rev.texto)
        return lista

    async def executar(self, caminho_prd: Path) -> str:
        p = prdmod.ler(caminho_prd)
        try:
            estado = EstadoPrd.carregar(self.raiz, p.id)
        except EstadoCorrompido as e:
            prdmod.mudar_status(caminho_prd, "estacionado")
            _inbox(self.raiz, p.id, str(e))
            self.registro(f"estacionado: {e}", prd=p.id, papel="firma")
            return "estacionado"
        base = self.cfg["projeto"].get("branch_principal") or "main"
        github = (self.cfg["projeto"].get("github") or "").strip()
        usar_pr = bool(github) and not self.sem_pr
        branch = f"firma/{p.id}"
        docs = f"docs/firma/{p.id}"
        log = lambda texto, papel="firma": self.registro(texto, prd=p.id, papel=papel)  # noqa: E731

        prdmod.mudar_status(caminho_prd, "em-execucao")
        lista: list[tuple[str, str, bool]] = []
        wt = self.raiz
        try:
            wt = gitops.preparar_worktree(self.raiz, p.id, base)
            log(f"worktree {wt} na branch {branch}")
            ligados = gitops.ligar_ambientes(self.raiz, wt, self.partes)
            if ligados:
                log("ambientes ligados do repositório principal: " + ", ".join(ligados))
            self._ctx = _Contexto(p.id, branch, wt, docs, estado)
            (wt / docs).mkdir(parents=True, exist_ok=True)
            (wt / docs / "prd.md").write_text(caminho_prd.read_text(encoding="utf-8-sig"), encoding="utf-8")
            prd_texto = (wt / docs / "prd.md").read_text(encoding="utf-8")

            if estado.fase == "spec":
                lista = await self._fase_spec(p, wt, docs, estado, prd_texto, log)
                gitops.commit(wt, f"docs({p.id}): spec e plano\n\nGerado pela Firma.")
                estado.fase = "tarefas"
                estado.salvar(self.raiz, p.id)
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
                estado.limpar_tarefa()
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
            return self._estacionar(p, caminho_prd, estado, wt, usar_pr, lista, str(e), log)
        except Exception as e:  # erro inesperado: não derruba o turno, estaciona com o detalhe
            detalhe = traceback.format_exc()
            self.registro(f"erro inesperado: {type(e).__name__}: {e}", prd=p.id, papel="firma", traceback=detalhe)
            return self._estacionar(
                p, caminho_prd, estado, wt, usar_pr, lista,
                f"Erro inesperado na Firma ({type(e).__name__}: {e}). Detalhe no log de ~/.firma/logs.", log,
            )
        except BaseException as e:  # Ctrl+C ou cancelamento: devolve o PRD para a fila e sai
            estado.salvar(self.raiz, p.id)
            prdmod.mudar_status(caminho_prd, "pronto")
            log(f"interrompido ({type(e).__name__}): o PRD volta para a fila")
            raise
        finally:
            self._ctx = None

    def _estacionar(self, p, caminho_prd, estado, wt, usar_pr, lista, motivo, log) -> str:
        estado.salvar(self.raiz, p.id)
        prdmod.mudar_status(caminho_prd, "estacionado")
        _inbox(self.raiz, p.id, motivo)
        if usar_pr and estado.pr:
            try:
                gitops.atualizar_pr(wt, estado.pr, self._corpo_pr(p, estado, lista, f"Estacionado: {motivo}"))
            except Exception:
                pass
        log(f"estacionado: {motivo}")
        return "estacionado"

    async def _fazer_tarefa(self, p, wt: Path, docs: str, tarefa, estado: EstadoPrd) -> None:
        rodadas = int(self.cfg["execucao"].get("rodadas", 3))
        tid = tarefa[0]
        retomada = estado.tarefa_ativa == tid
        if not retomada:
            estado.limpar_tarefa()
            estado.tarefa_ativa = tid
        if self._ctx:
            self._ctx.tarefa = (tid, tarefa[1])
        retorno = estado.ultimo_retorno
        historico = list(estado.historico_rodadas)
        handoff_pendente = "retomando uma tarefa interrompida" if retomada and gitops.tem_mudancas_codigo(wt, docs) else None
        if retomada:
            self.registro(f"{tid}: retomando na rodada {estado.rodada_atual}", prd=p.id, papel="firma")

        def guardar(rodada: int) -> None:
            estado.rodada_atual, estado.ultimo_retorno, estado.historico_rodadas = rodada, retorno, list(historico)
            estado.salvar(self.raiz, p.id)

        rodada = max(1, estado.rodada_atual)
        diag_pendente: str | None = None
        while rodada <= rodadas + 1:  # a última só acontece depois do diagnóstico do advisor
            guardar(rodada)
            if rodada == rodadas + 1 and not (retorno or "").startswith("Diagnóstico do advisor"):
                diag = await self._advisor(p, wt, _prompt_diagnostico(tarefa, docs, historico), estado)
                if diag.texto.strip().upper().startswith("BLOQUEIO"):
                    _anotar(wt / docs / "decisoes.md", f"Diagnóstico da {tid}", diag.texto)
                    raise Estacionar(f"{tid}: {diag.texto.strip()[:600]}")
                estado.decisoes.append(f"{tid}: última rodada com o diagnóstico do advisor")
                retorno = f"Diagnóstico do advisor (última chance):\n{diag.texto}"
                diag_pendente = diag.texto  # anotado em decisoes.md só depois do developer
                guardar(rodada)

            await self.sessao(
                "developer", _prompt_developer(tarefa, docs, self.partes, retorno), wt, p.id, estado,
                retomar=True, com_handoff=handoff_pendente, escrita_proibida=[docs],
            )
            handoff_pendente = None
            estado.salvar(self.raiz, p.id)

            if diag_pendente:
                _anotar(wt / docs / "decisoes.md", f"Diagnóstico da {tid}", diag_pendente)
                diag_pendente = None

            if not gitops.tem_mudancas_codigo(wt, docs):
                retorno = "Nenhum arquivo foi alterado. Implemente a tarefa."
                historico.append(f"Rodada {rodada}: o developer não alterou nada.")
                rodada += 1
                continue

            verificacoes = gitops.rodar_verificacoes(wt, self.partes, enquanto=renovar_trava)
            texto_verif = _texto_verificacoes(verificacoes)
            if any(not v.ok for v in verificacoes):
                self.registro(f"verificações falharam (rodada {rodada})", prd=p.id, papel="firma")
                retorno = f"As verificações falharam:\n{texto_verif}"
                historico.append(f"Rodada {rodada}: verificações falharam.\n{texto_verif[-1500:]}")
                rodada += 1
                continue

            retrato = gitops.foto(wt)
            aud = await self.sessao(
                "auditor", _prompt_auditor(tarefa, docs, gitops.diff(wt), texto_verif), wt, p.id, estado,
                retomar=True, pode_escrever=False, ferramentas=LEITURA,
            )
            self._so_leitura(wt, retrato, "auditor", p.id)
            if veredito(aud.texto):
                self.registro(f"{tid} aprovada", prd=p.id, papel="auditor")
                return
            self.registro(f"{tid} reprovada (rodada {rodada})", prd=p.id, papel="auditor")
            retorno = aud.texto
            historico.append(f"Rodada {rodada}: auditor reprovou.\n{aud.texto[-2000:]}")
            rodada += 1

        guardar(rodada)
        raise Estacionar(f"{tid} reprovada em todas as rodadas, inclusive depois do diagnóstico do advisor")


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
    caminho.parent.mkdir(parents=True, exist_ok=True)
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
    # Um PRD em "em-execucao" quando nenhum turno está rodando ficou órfão (queda, Ctrl+C):
    # volta para a fila e retoma do estado salvo.
    prds = [p for p in prdmod.listar(backlog) if p.status in ("pronto", "em-execucao")]
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
    from firma import config, registro as reg

    if trava_ativa():
        raise RuntimeError("já existe um `firma run` rodando nesta máquina (veja `firma status`)")
    criar_trava()
    arquivo_parar().unlink(missing_ok=True)

    quota = Quota()
    esgotados: set[str] = set()
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
            turno = Turno(raiz, cfg, registro, quota, rodar_sessao, sem_pr, esgotados)
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
    except Exception as e:
        resumo["erro"] = f"{type(e).__name__}: {e}"
        registro(f"erro no turno: {resumo['erro']}", traceback=traceback.format_exc())
        return resumo
    finally:
        soltar_trava()
        arquivo_parar().unlink(missing_ok=True)
        registro(
            f"fim do turno: {len(resumo['pr-aberto'])} PR(s), {len(resumo['estacionado'])} estacionado(s)"
            + (f", parou por: {resumo['pausa']}" if resumo["pausa"] else "")
            + (f", erro: {resumo['erro']}" if resumo.get("erro") else "")
            + f" · quota semana {_pct(quota.semanal)}"
            + (f" · sem quota: {', '.join(sorted(esgotados))}" if esgotados else "")
        )
