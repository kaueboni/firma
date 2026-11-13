"""Uma sessão de agente no OpenCode (Gemini e outros provedores com credencial própria).

Formatos confirmados na sonda contra o OpenCode 1.19.5 (`firma doctor --opencode`):
- `opencode serve --hostname 127.0.0.1 --port N`; senha em OPENCODE_SERVER_PASSWORD (basic auth,
  usuário `opencode`); config inteira em OPENCODE_CONFIG_CONTENT (JSON).
- Um servidor atende várias worktrees: todo endpoint aceita `?directory=<pasta>`.
- POST /session {title, permission: [{permission, pattern, action}]} → {id}
- POST /session/{id}/message {agent, model: {providerID, modelID}, parts: [{type: text, text}]}
  responde quando a sessão termina: {info: AssistantMessage (error?, tokens), parts}.
- GET /event (SSE, `data: {type, properties}`): message.part.updated (part.type == "tool", com
  state.status running/completed/error), session.status ({type: idle|busy|retry, attempt, message}),
  session.error ({error}), permission.asked ({id}) → POST /permission/{id}/reply {reply: reject}.
- Erros: APIError {statusCode, message}, ProviderAuthError, UnknownError; provedor sem credencial ou
  modelo inexistente dão HTTP 500 sem detalhe, então a Firma confere antes em GET /provider.
- O plugin carrega todas as funções exportadas como plugin; skills globais do usuário (~/.claude/skills)
  aparecem para o agente se não forem negadas por sessão.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Callable

import anyio
import httpx

from firma.registro import home_firma
from firma.runtime import erros, opencode_config as occ
from firma.runtime.base import Feed, Pedido, Resposta, caminho_curto
from firma.trava import renovar_trava

OPENCODE_MIN_VERSION = (1, 19, 0)
TENTATIVAS_RETRY_ATE_QUOTA = 3  # o OpenCode tenta de novo sozinho; depois disso é limite da conta
PASTAS_SKILLS = (".claude/skills", ".opencode/skills", ".agents/skills")


class ErroOpencode(RuntimeError):
    pass


def versao(texto: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in texto.strip().split("-")[0].split(".")[:3])
    except ValueError:
        return ()


def comando_opencode() -> list[str] | None:
    caminho = shutil.which("opencode")
    if not caminho:
        return None
    if caminho.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", caminho]
    return [caminho]


def skills_do_projeto(raiz: Path) -> list[str]:
    """Nomes das skills do projeto: só elas ficam liberadas (as globais do usuário não)."""
    nomes = []
    for pasta in PASTAS_SKILLS:
        base = raiz / pasta
        if not base.is_dir():
            continue
        for item in sorted(base.iterdir()):
            if item.is_dir() and (item / "SKILL.md").exists():
                nomes.append(_nome_skill(item / "SKILL.md") or item.name)
            elif item.suffix == ".md":
                nomes.append(_nome_skill(item) or item.stem)
    return sorted(set(nomes))


def _nome_skill(arquivo: Path) -> str | None:
    try:
        texto = arquivo.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    if texto.startswith("---"):
        for linha in texto.split("---", 2)[1].splitlines():
            if linha.strip().startswith("name:"):
                return linha.split(":", 1)[1].strip().strip("'\"") or None
    return None


def regras_skills(nomes: list[str]) -> list[dict]:
    return [{"permission": "skill", "pattern": "*", "action": "deny"}] + [
        {"permission": "skill", "pattern": n, "action": "allow"} for n in nomes
    ]


def resumo_ferramenta(part: dict, cwd: Path) -> str | None:
    """Uma linha para o feed quando uma ferramenta começa (ou é recusada)."""
    estado = part.get("state") or {}
    nome = part.get("tool") or "?"
    entrada = estado.get("input") or {}
    if estado.get("status") == "error" and "Bloqueado pela Firma" in str(estado.get("error", "")):
        return f"{nome} recusado: {estado['error']}"
    if estado.get("status") != "running":
        return None
    if nome in ("bash", "shell"):
        return f"Bash: {str(entrada.get('command', ''))[:120]}"
    for campo in ("filePath", "file_path", "path"):
        if entrada.get(campo):
            return f"{nome} {caminho_curto(entrada[campo], cwd)}"
    for campo in ("pattern", "name", "query", "url"):
        if entrada.get(campo):
            return f"{nome} {entrada[campo]}"
    return nome


def classificar_erro(erro: dict | None) -> tuple[str, str] | None:
    """(tipo, mensagem) a partir do `error` de uma AssistantMessage ou de session.error."""
    if not erro:
        return None
    nome = erro.get("name", "")
    dados = erro.get("data") or {}
    msg = str(dados.get("message") or nome)
    if nome == "ProviderAuthError":
        return erros.AUTENTICACAO, msg
    if nome == "APIError":
        corpo = str(dados.get("responseBody") or "")
        return erros.classificar(dados.get("statusCode"), f"{msg} {corpo}"), msg
    if nome == "MessageAbortedError":
        return erros.DESCONHECIDO, "sessão abortada"
    if nome in ("ContextOverflowError", "MessageOutputLengthError", "ContentFilterError", "StructuredOutputError"):
        return erros.CONFIGURACAO, f"{nome}: {msg}"
    return erros.classificar(None, msg), msg


def _porta_livre() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    porta = s.getsockname()[1]
    s.close()
    return porta


class HostOpencode:
    """Um `opencode serve` local por `firma run`, subido na primeira sessão que precisa dele."""

    def __init__(self, registro: Callable, detalhe: bool = False, transporte: httpx.AsyncBaseTransport | None = None):
        self.registro, self.detalhe = registro, detalhe
        self._transporte = transporte  # testes: servidor falso, sem processo
        self._proc: subprocess.Popen | None = None
        self._cliente: httpx.AsyncClient | None = None
        self._sessao_ativa: tuple[str, str] | None = None  # (session_id, directory)
        self.estado_plugin = occ.EstadoPlugin(home_firma() / "opencode" / f"estado-{os.getpid()}.json")

    # ------------------------------------------------------------ ciclo de vida

    async def iniciar(self) -> None:
        if self._cliente is not None:
            return
        if self._transporte is not None:
            self._cliente = httpx.AsyncClient(base_url="http://opencode", transport=self._transporte, timeout=None)
            return
        cmd = comando_opencode()
        if cmd is None:
            raise ErroOpencode("`opencode` não encontrado no PATH (npm install -g opencode-ai)")
        porta, senha = _porta_livre(), secrets.token_hex(24)
        env = {
            **os.environ,
            "OPENCODE_CONFIG_CONTENT": json.dumps(occ.config()),
            "OPENCODE_SERVER_PASSWORD": senha,
            "FIRMA_OPENCODE_ESTADO": str(self.estado_plugin.caminho),
        }
        pasta_log = home_firma() / "opencode"
        pasta_log.mkdir(parents=True, exist_ok=True)
        log = open(pasta_log / "serve.log", "ab")
        self._proc = subprocess.Popen(
            [*cmd, "serve", "--hostname", "127.0.0.1", "--port", str(porta), "--print-logs", "--log-level", "WARN"],
            env=env, stdout=log, stderr=subprocess.STDOUT, cwd=pasta_log,
        )
        self._cliente = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{porta}", auth=("opencode", senha), timeout=httpx.Timeout(30, read=None)
        )
        inicio = time.monotonic()
        while time.monotonic() - inicio < 60:
            if self._proc.poll() is not None:
                raise ErroOpencode(f"o opencode serve saiu ao iniciar (veja {pasta_log / 'serve.log'})")
            try:
                r = await self._cliente.get("/global/health")
                if r.status_code == 200:
                    v = versao(r.json().get("version", ""))
                    if v and v < OPENCODE_MIN_VERSION:
                        raise ErroOpencode(f"OpenCode {r.json().get('version')} é antigo; precisa de {'.'.join(map(str, OPENCODE_MIN_VERSION))}+")
                    self.registro(f"OpenCode {r.json().get('version')} em 127.0.0.1:{porta}", papel="firma")
                    return
            except httpx.TransportError:
                pass
            await anyio.sleep(0.5)
        raise ErroOpencode("o opencode serve não respondeu em 60s")

    async def fechar(self) -> None:
        if self._sessao_ativa and self._cliente is not None:
            await self._abortar(*self._sessao_ativa)
        if self._cliente is not None:
            await self._cliente.aclose()
            self._cliente = None
        if self._proc is not None and self._proc.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self._proc.pid)], capture_output=True)
            else:
                self._proc.terminate()
                try:
                    self._proc.wait(10)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        self._proc = None
        self.estado_plugin.caminho.unlink(missing_ok=True)

    async def _abortar(self, session_id: str, directory: str) -> None:
        with anyio.move_on_after(5, shield=True):
            try:
                await self._cliente.post(f"/session/{session_id}/abort", params={"directory": directory})
            except httpx.HTTPError:
                pass

    # ------------------------------------------------------------ checagens

    async def provedores(self, directory: str) -> dict:
        r = await self._cliente.get("/provider", params={"directory": directory})
        r.raise_for_status()
        return r.json()

    async def _conferir_modelo(self, pedido: Pedido, directory: str) -> Resposta | None:
        try:
            dados = await self.provedores(directory)
        except httpx.HTTPError as e:
            return self._falha(pedido, erros.REDE, f"não consegui falar com o OpenCode: {e}")
        if pedido.provider not in (dados.get("connected") or []):
            return self._falha(
                pedido, erros.AUTENTICACAO,
                f"o provedor {pedido.provider} não está conectado no OpenCode (rode `opencode auth login`)",
            )
        modelos = next((p.get("models") or {} for p in dados.get("all", []) if p.get("id") == pedido.provider), {})
        if pedido.modelo and modelos and pedido.modelo not in modelos:
            return self._falha(pedido, erros.CONFIGURACAO, f"o modelo {pedido.provider}/{pedido.modelo} não existe no OpenCode")
        return None

    def _falha(self, pedido: Pedido, tipo: str, msg: str) -> Resposta:
        return Resposta(erro=msg, tipo_erro=tipo, sem_quota=tipo == erros.QUOTA, provider=pedido.provider, modelo=pedido.modelo or "")

    # ------------------------------------------------------------ sessão

    async def _sessao(self, pedido: Pedido, directory: str) -> str:
        if pedido.resume:
            r = await self._cliente.get(f"/session/{pedido.resume}", params={"directory": directory})
            if r.status_code == 200:
                return pedido.resume
        regras = occ.regras_da_sessao(pedido.papel, pedido.pode_escrever, pedido.escrita_so_em, pedido.escrita_proibida)
        regras += regras_skills(skills_do_projeto(pedido.cwd))
        r = await self._cliente.post(
            "/session", params={"directory": directory},
            json={"title": f"Firma {pedido.prd} · {pedido.papel}", "permission": regras},
        )
        r.raise_for_status()
        return r.json()["id"]

    async def rodar(self, pedido: Pedido) -> Resposta:
        """Roda a sessão; sobrecarga momentânea do provedor tenta de novo (5s, 15s) na mesma sessão."""
        esperas = list(erros.ESPERAS_TRANSITORIO)
        while True:
            r = await self._rodar_uma(pedido)
            if r.tipo_erro != erros.TRANSITORIO or not esperas:
                return r
            espera = esperas.pop(0)
            self.registro(f"{pedido.provider} sobrecarregado: nova tentativa em {espera}s", prd=pedido.prd, papel=pedido.papel)
            await anyio.sleep(espera)
            if r.session_id:
                pedido.resume = r.session_id

    async def _rodar_uma(self, pedido: Pedido) -> Resposta:
        if not pedido.modelo:
            return self._falha(pedido, erros.CONFIGURACAO, f"o papel {pedido.papel} está em {pedido.provider} sem `model` no config.toml")
        try:
            await self.iniciar()
        except ErroOpencode as e:
            return self._falha(pedido, erros.CONFIGURACAO, str(e))
        directory = str(pedido.cwd)
        falha = await self._conferir_modelo(pedido, directory)
        if falha:
            return falha

        feed = Feed(self.registro, pedido.prd, pedido.papel)
        inicio = time.monotonic()
        resposta = Resposta(provider=pedido.provider, modelo=pedido.modelo or "")
        try:
            session_id = await self._sessao(pedido, directory)
        except httpx.HTTPError as e:
            return self._falha(pedido, erros.REDE, f"OpenCode: {e}")
        resposta.session_id = session_id
        self.estado_plugin.registrar(
            session_id, pedido.papel, pedido.cwd, pedido.pode_escrever, pedido.escrita_so_em, pedido.escrita_proibida
        )
        self._sessao_ativa = (session_id, directory)
        motivo_aborto: list[tuple[str, str]] = []
        conectado = anyio.Event()
        vistos: set[tuple[str, str]] = set()  # (callID, status): o OpenCode repete o mesmo estado

        async def ouvir() -> None:
            try:
                async with self._cliente.stream("GET", "/event", params={"directory": directory}) as r:
                    conectado.set()
                    async for linha in r.aiter_lines():
                        if not linha.startswith("data:"):
                            continue
                        try:
                            ev = json.loads(linha[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        renovar_trava()
                        await self._tratar_evento(ev, session_id, directory, pedido, feed, resposta, motivo_aborto, vistos)
            except httpx.HTTPError as e:  # sem feed não impede a sessão; a resposta final ainda chega
                self.registro(f"feed do OpenCode caiu: {e}", prd=pedido.prd, papel=pedido.papel)
            finally:
                conectado.set()

        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(ouvir)
                with anyio.move_on_after(10):  # assina os eventos antes de mandar o prompt
                    await conectado.wait()
                corpo = {
                    "agent": occ.nome_agente(pedido.papel),
                    "model": {"providerID": pedido.provider, "modelID": pedido.modelo},
                    "parts": [{"type": "text", "text": pedido.prompt}],
                }
                try:
                    r = await self._cliente.post(f"/session/{session_id}/message", params={"directory": directory}, json=corpo)
                finally:
                    tg.cancel_scope.cancel()
            self._interpretar(r, resposta, motivo_aborto)
        except httpx.HTTPError as e:
            resposta.erro, resposta.tipo_erro = f"OpenCode: {e}", erros.REDE
        except BaseException:
            await self._abortar(session_id, directory)
            raise
        finally:
            self._sessao_ativa = None
            feed.fechar()
        minutos = (time.monotonic() - inicio) / 60
        self.registro(
            f"terminou · {minutos:.0f} min · {resposta.tokens / 1000:.0f}k tokens · {pedido.provider}/{pedido.modelo}",
            prd=pedido.prd, papel=pedido.papel, tokens=resposta.tokens, minutos=round(minutos, 1), provider=pedido.provider,
        )
        return resposta

    async def _tratar_evento(self, ev, session_id, directory, pedido, feed, resposta, motivo_aborto, vistos=None) -> None:
        tipo, props = ev.get("type"), ev.get("properties") or {}
        if props.get("sessionID") not in (None, session_id) and (props.get("part") or {}).get("sessionID") != session_id:
            return
        if tipo == "message.part.updated":
            part = props.get("part") or {}
            if part.get("sessionID") != session_id:
                return
            if part.get("type") == "tool":
                chave = (str(part.get("callID") or part.get("id")), str((part.get("state") or {}).get("status")))
                if vistos is not None:
                    if chave in vistos:
                        return
                    vistos.add(chave)
                linha = resumo_ferramenta(part, pedido.cwd)
                if linha:
                    if "recusado" in linha:
                        resposta.bloqueios.append(linha.split("recusado: ", 1)[1])
                    feed(linha)
            elif part.get("type") == "text" and self.detalhe and (part.get("time") or {}).get("end"):
                feed(str(part.get("text", "")).strip()[:400])
        elif tipo == "permission.asked" and props.get("sessionID") == session_id:
            # Nenhuma regra é "ask"; se aparecer uma pergunta, é configuração errada: recusa e registra.
            try:
                await self._cliente.post(f"/permission/{props.get('id')}/reply", params={"directory": directory}, json={"reply": "reject"})
            except httpx.HTTPError:
                pass
            resposta.bloqueios.append(f"pedido de permissão inesperado ({props.get('permission')}) recusado")
            if not resposta.erro:
                resposta.erro = f"o OpenCode pediu permissão interativa para {props.get('permission')}; revise as regras da Firma"
                resposta.tipo_erro = erros.CONFIGURACAO
        elif tipo == "session.status" and props.get("sessionID") == session_id:
            status = props.get("status") or {}
            if status.get("type") == "retry":
                msg = str(status.get("message") or "")
                classe = erros.classificar(None, msg)
                feed(f"OpenCode tentando de novo ({status.get('attempt')}): {msg[:120]}")
                if classe == erros.QUOTA or int(status.get("attempt") or 0) >= TENTATIVAS_RETRY_ATE_QUOTA:
                    motivo_aborto.append((erros.QUOTA if classe in (erros.QUOTA, erros.TRANSITORIO) else classe, msg))
                    await self._abortar(session_id, directory)

    def _interpretar(self, r: httpx.Response, resposta: Resposta, motivo_aborto: list[tuple[str, str]]) -> None:
        try:
            corpo = r.json()
        except ValueError:
            corpo = {}
        if r.status_code >= 400:
            classe = classificar_erro(corpo if "name" in corpo else None)
            resposta.erro = f"OpenCode HTTP {r.status_code}: {(classe or ('', r.text[:300]))[1]}"
            resposta.tipo_erro = classe[0] if classe else erros.DESCONHECIDO
        info = corpo.get("info") or {}
        tokens = info.get("tokens") or {}
        resposta.tokens = int(tokens.get("total") or (tokens.get("input", 0) + tokens.get("output", 0)) or 0)
        textos = [p.get("text", "") for p in corpo.get("parts") or [] if p.get("type") == "text" and p.get("text", "").strip()]
        resposta.texto = textos[-1] if textos else ""
        classe = classificar_erro(info.get("error"))
        if motivo_aborto:
            classe = motivo_aborto[0]
        if classe and not (resposta.erro and resposta.tipo_erro == erros.CONFIGURACAO):
            resposta.tipo_erro, resposta.erro = classe[0], classe[1] or resposta.erro
        if resposta.tipo_erro == erros.QUOTA:
            resposta.sem_quota = True


# ---------------------------------------------------------------- sonda (firma doctor --opencode)


async def sondar(pasta: Path, modelos: list[tuple[str, str]], registro: Callable = lambda *a, **k: None) -> dict:
    """Sobe o OpenCode, confere provedores e modelos e faz uma chamada mínima em cada modelo.
    Nunca grava chaves: só ids de provedor/modelo, versões e mensagens de erro."""
    host = HostOpencode(registro)
    resultado: dict = {"pasta": str(pasta), "modelos": []}
    try:
        await host.iniciar()
        saude = (await host._cliente.get("/global/health")).json()
        resultado["versao"] = saude.get("version")
        dados = await host.provedores(str(pasta))
        resultado["conectados"] = dados.get("connected") or []
        for provider, modelo in modelos:
            item = {"provider": provider, "model": modelo}
            pedido = Pedido("advisor", "Responda apenas com a palavra: ok", pasta, "sonda", provider=provider, modelo=modelo, pode_escrever=False)
            r = await host.rodar(pedido)
            item.update(ok=r.erro is None, erro=r.erro, tipo_erro=r.tipo_erro, resposta=r.texto[:80], tokens=r.tokens)
            resultado["modelos"].append(item)
    except (ErroOpencode, httpx.HTTPError) as e:
        resultado["erro"] = str(e)
    finally:
        await host.fechar()
    return resultado


def salvar_sonda(resultado: dict) -> Path:
    pasta = home_firma() / "sondas"
    pasta.mkdir(parents=True, exist_ok=True)
    caminho = pasta / f"opencode-{time.strftime('%Y%m%d-%H%M%S')}.json"
    caminho.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    return caminho
