"""Leitura do .firma/config.toml de um projeto, com os valores padrão.

Modelos por papel: `[modelos.tech_lead]`, `[modelos.advisor]`, `[modelos.developer]`,
`[modelos.auditor]`, cada um com `provider`, `model` e, opcionalmente, `fallback_provider` e
`fallback_model`. O formato antigo (`[modelos]` com `trabalho` e `advisor` como texto) é
convertido em memória, sem reescrever o arquivo.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

PROVEDORES = ("anthropic", "google")
FALLBACK_PADRAO = ("google", "gemini-2.5-pro")

PADRAO = {
    "projeto": {"nome": "", "branch_principal": "main", "github": ""},
    "partes": [],
    "quota": {"teto_semanal": 0.6, "janela": ""},
    "execucao": {
        # Modo de permissão das sessões do Claude. As regras da Firma (guarda.py) valem em qualquer modo.
        "permissao": "bypassPermissions",
        "rodadas": 3,
    },
}

# papel (como aparece no código) → seção no config.toml
SECOES_PAPEL = {"tech-lead": "tech_lead", "advisor": "advisor", "developer": "developer", "auditor": "auditor"}


@dataclass(frozen=True)
class ModeloPapel:
    provider: str
    model: str
    fallback_provider: str = ""
    fallback_model: str = ""

    @property
    def tem_fallback(self) -> bool:
        return bool(self.fallback_provider and self.fallback_model)


def _padrao_modelos(trabalho: str = "", advisor: str = "opus") -> dict:
    fp, fm = FALLBACK_PADRAO
    base = {"provider": "anthropic", "fallback_provider": fp, "fallback_model": fm}
    return {
        "tech_lead": {**base, "model": trabalho},
        "advisor": {**base, "model": advisor},
        "developer": {**base, "model": trabalho},
        "auditor": {**base, "model": trabalho},
    }


def _modelos(dados: dict) -> dict:
    bruto = dados.get("modelos")
    if not isinstance(bruto, dict):
        return _padrao_modelos()
    novos = {k: v for k, v in bruto.items() if isinstance(v, dict)}
    if not novos:  # formato antigo: só textos
        return _padrao_modelos(str(bruto.get("trabalho") or ""), str(bruto.get("advisor") or "opus"))
    final = _padrao_modelos()
    for secao, valor in novos.items():
        if secao in final:
            mistura = {**final[secao], **valor}
            if "fallback_provider" not in valor and "fallback_model" not in valor and valor.get("provider", "anthropic") != "anthropic":
                mistura["fallback_provider"] = mistura["fallback_model"] = ""  # fallback padrão só vale para o Claude
            final[secao] = mistura
    return final


def carregar(raiz: Path) -> dict:
    caminho = raiz / ".firma" / "config.toml"
    dados = tomllib.loads(caminho.read_text(encoding="utf-8-sig"))
    final: dict = {}
    for secao, padrao in PADRAO.items():
        valor = dados.get(secao, padrao)
        if isinstance(padrao, dict) and isinstance(valor, dict):
            final[secao] = {**padrao, **valor}
        else:
            final[secao] = valor
    final["modelos"] = _modelos(dados)
    return final


def modelo_do_papel(cfg: dict, papel: str) -> ModeloPapel:
    secao = SECOES_PAPEL.get(papel, papel)
    m = (cfg.get("modelos") or {}).get(secao) or _padrao_modelos()[secao]
    return ModeloPapel(
        provider=str(m.get("provider") or "anthropic"),
        model=str(m.get("model") or ""),
        fallback_provider=str(m.get("fallback_provider") or ""),
        fallback_model=str(m.get("fallback_model") or ""),
    )


def precisa_opencode(cfg: dict) -> bool:
    """Algum papel usa (ou pode cair para) um provedor que roda no OpenCode."""
    for papel in SECOES_PAPEL:
        m = modelo_do_papel(cfg, papel)
        if m.provider != "anthropic" or (m.tem_fallback and m.fallback_provider != "anthropic"):
            return True
    return False
