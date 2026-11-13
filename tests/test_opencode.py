"""Runtime OpenCode contra um servidor falso (httpx.MockTransport), sem processo nem rede."""

import json
import re
import subprocess
import shutil

import anyio
import httpx
import pytest

from firma import guarda
from firma.runtime import erros, opencode, opencode_config as occ
from firma.runtime.base import Pedido


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FIRMA_HOME", str(tmp_path / "home"))


def _sse(*eventos):
    return "".join(f"data: {json.dumps(e)}\n\n" for e in eventos).encode()


class Servidor:
    def __init__(self, resposta=None, eventos=(), conectados=("google",), status=200):
        self.resposta = resposta or {
            "info": {"tokens": {"total": 1234, "input": 1000, "output": 234}},
            "parts": [{"type": "text", "text": "feito\nVEREDITO: APROVADO"}],
        }
        self.eventos, self.conectados, self.status = list(eventos), list(conectados), status
        self.chamadas = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.chamadas.append((req.method, req.url.path, json.loads(req.content or b"null") if req.method == "POST" else None))
        caminho = req.url.path
        if caminho == "/provider":
            return httpx.Response(200, json={"connected": self.conectados, "all": [{"id": "google", "models": {"gemini-2.5-pro": {}}}]})
        if caminho == "/session" and req.method == "POST":
            return httpx.Response(200, json={"id": "ses_1"})
        if caminho == "/event":
            return httpx.Response(200, content=_sse(*self.eventos), headers={"content-type": "text/event-stream"})
        if caminho.endswith("/message"):
            return httpx.Response(self.status, json=self.resposta)
        if caminho.endswith("/abort") or "/permission/" in caminho:
            return httpx.Response(200, json=True)
        return httpx.Response(404, json={})


def _rodar(servidor, tmp_path, **kw):
    linhas = []
    host = opencode.HostOpencode(lambda t, **k: linhas.append(t), transporte=httpx.MockTransport(servidor))
    pedido = Pedido("auditor", "audite", tmp_path, "PRD-001", provider="google", modelo="gemini-2.5-pro", pode_escrever=False, **kw)

    async def principal():
        try:
            return await host.rodar(pedido)
        finally:
            await host.fechar()

    return anyio.run(principal), linhas, servidor


def test_sessao_ok_com_feed_e_bloqueio(tmp_path):
    eventos = [
        {"type": "message.part.updated", "properties": {"sessionID": "ses_1", "part": {"sessionID": "ses_1", "type": "tool", "tool": "read", "state": {"status": "running", "input": {"filePath": str(tmp_path / "app.py")}}}}},
        {"type": "message.part.updated", "properties": {"sessionID": "ses_1", "part": {"sessionID": "ses_1", "type": "tool", "tool": "edit", "state": {"status": "error", "error": "Bloqueado pela Firma: este papel não edita arquivos."}}}},
        {"type": "message.part.updated", "properties": {"sessionID": "outra", "part": {"sessionID": "outra", "type": "tool", "tool": "bash", "state": {"status": "running", "input": {"command": "ls"}}}}},
    ]
    r, linhas, srv = _rodar(Servidor(eventos=eventos), tmp_path)
    assert r.erro is None and r.texto.endswith("APROVADO") and r.tokens == 1234 and r.session_id == "ses_1"
    assert "read app.py" in linhas and any("recusado" in l for l in linhas)
    assert not any("Bash: ls" in l for l in linhas)  # evento de outra sessão
    assert r.bloqueios == ["Bloqueado pela Firma: este papel não edita arquivos."]
    criada = next(c for c in srv.chamadas if c[1] == "/session")[2]
    assert {"permission": "edit", "pattern": "*", "action": "deny"} in criada["permission"]
    assert {"permission": "skill", "pattern": "*", "action": "deny"} in criada["permission"]
    corpo = next(c for c in srv.chamadas if c[1].endswith("/message"))[2]
    assert corpo["agent"] == "firma-auditor" and corpo["model"] == {"providerID": "google", "modelID": "gemini-2.5-pro"}


def test_provedor_desconectado_e_modelo_inexistente(tmp_path):
    r, _, srv = _rodar(Servidor(conectados=["opencode"]), tmp_path)
    assert r.tipo_erro == erros.AUTENTICACAO and not any(c[1].endswith("/message") for c in srv.chamadas)
    linhas = []
    host = opencode.HostOpencode(lambda t, **k: linhas.append(t), transporte=httpx.MockTransport(Servidor()))
    pedido = Pedido("developer", "x", tmp_path, "P", provider="google", modelo="gemini-9")
    r = anyio.run(host.rodar, pedido)
    assert r.tipo_erro == erros.CONFIGURACAO


def test_erro_de_quota_da_api(tmp_path):
    resposta = {"info": {"error": {"name": "APIError", "data": {"message": "Resource has been exhausted (e.g. check quota).", "statusCode": 429, "isRetryable": True}}}, "parts": []}
    r, _, _ = _rodar(Servidor(resposta=resposta), tmp_path)
    assert r.tipo_erro == erros.QUOTA and r.sem_quota


def test_retry_repetido_vira_quota_e_aborta(tmp_path):
    eventos = [{"type": "session.status", "properties": {"sessionID": "ses_1", "status": {"type": "retry", "attempt": a, "message": "Too Many Requests", "next": 0}}} for a in (1, 2, 3)]
    resposta = {"info": {"error": {"name": "MessageAbortedError", "data": {"message": "aborted"}}}, "parts": []}
    r, _, srv = _rodar(Servidor(eventos=eventos, resposta=resposta), tmp_path)
    assert r.tipo_erro == erros.QUOTA and r.sem_quota
    assert any(c[1].endswith("/abort") for c in srv.chamadas)


def test_pedido_de_permissao_e_recusado(tmp_path):
    eventos = [{"type": "permission.asked", "properties": {"id": "per_1", "sessionID": "ses_1", "permission": "bash", "patterns": ["ls"], "metadata": {}, "always": []}}]
    r, _, srv = _rodar(Servidor(eventos=eventos), tmp_path)
    resposta = next(c for c in srv.chamadas if "/permission/" in c[1])
    assert resposta[2] == {"reply": "reject"}
    assert r.tipo_erro == erros.CONFIGURACAO


def test_erro_de_autenticacao_do_provedor(tmp_path):
    resposta = {"info": {"error": {"name": "ProviderAuthError", "data": {"providerID": "google", "message": "API key not valid"}}}, "parts": []}
    r, _, _ = _rodar(Servidor(resposta=resposta), tmp_path)
    assert r.tipo_erro == erros.AUTENTICACAO and not r.sem_quota


def test_config_sem_ask():
    cfg = occ.config()

    def valores(x):
        if isinstance(x, dict):
            for v in x.values():
                yield from valores(v)
        else:
            yield x

    assert "ask" not in set(valores(cfg["permission"]))
    for agente in cfg["agent"].values():
        assert "ask" not in set(valores(agente["permission"]))
    assert cfg["agent"]["firma-auditor"]["permission"]["edit"] == "deny"
    assert cfg["agent"]["firma-auditor"]["permission"]["bash"] == "deny"
    assert cfg["agent"]["firma-developer"]["permission"]["bash"] == "allow"
    assert cfg["plugin"][0].startswith("file:///") and cfg["plugin"][0].endswith("guarda.ts")
    for papel in ("tech-lead", "advisor", "developer", "auditor"):
        assert all(r["action"] in ("allow", "deny") for r in occ.regras_da_sessao(papel, papel in ("tech-lead", "developer"), None, []))


def test_skills_do_projeto(tmp_path):
    (tmp_path / ".claude/skills/stack").mkdir(parents=True)
    (tmp_path / ".claude/skills/stack/SKILL.md").write_text("---\nname: minha-stack\ndescription: x\n---\n")
    (tmp_path / ".opencode/skills/outra").mkdir(parents=True)
    (tmp_path / ".opencode/skills/outra/SKILL.md").write_text("sem frontmatter")
    assert opencode.skills_do_projeto(tmp_path) == ["minha-stack", "outra"]


def test_regras_do_guarda_compilam_em_js():
    """As regras de shell são lidas pelo plugin (JS): precisam ser regex válidas lá também."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node não instalado")
    regras = json.dumps([r["padrao"] for r in guarda.regras_para_plugin()])
    script = f"const rs={regras}; for (const r of rs) new RegExp(r, 'i'); const g=new RegExp(rs[0],'i'); process.stdout.write(String(g.test('git push origin')))"
    r = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "true"


def test_plugin_exporta_so_o_plugin():
    texto = occ.caminho_plugin().read_text(encoding="utf-8")
    assert re.findall(r"^export\s+\w+\s+(\w+)", texto, re.MULTILINE) == ["FirmaGuarda"]
