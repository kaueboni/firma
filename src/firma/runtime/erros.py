"""Classificação dos erros dos provedores: o que se tenta de novo, o que vira fallback e o que estaciona."""

from __future__ import annotations

import re

QUOTA = "quota"  # limite da assinatura/conta: troca para o fallback ou pausa
TRANSITORIO = "rate_limit_transitorio"  # sobrecarga ou burst: tenta de novo na mesma sessão
AUTENTICACAO = "autenticacao"
REDE = "rede"
CONFIGURACAO = "configuracao"
DESCONHECIDO = "desconhecido"
TIPOS = (QUOTA, TRANSITORIO, AUTENTICACAO, REDE, CONFIGURACAO, DESCONHECIDO)

# Esperas entre as tentativas de um erro transitório (segundos).
ESPERAS_TRANSITORIO = (5, 15)

_QUOTA = re.compile(
    r"usage limit|quota|limit reached|limite de uso|resource[_ ]exhausted|exceeded your current|"
    r"rate_limit_event|out of credits|billing",
    re.IGNORECASE,
)
_REDE = re.compile(
    r"getaddrinfo|ENOTFOUND|ECONNREFUSED|ECONNRESET|ETIMEDOUT|connection (refused|reset|error)|"
    r"network|dns|timed? ?out|unreachable",
    re.IGNORECASE,
)
_AUTENTICACAO = re.compile(r"unauthori[sz]ed|invalid (api )?key|api key not valid|authentication|forbidden|permission denied|expired", re.IGNORECASE)
_CONFIGURACAO = re.compile(r"model.*(not found|unavailable|does not exist)|unknown model|invalid request|bad request", re.IGNORECASE)


def classificar(status: int | None = None, mensagem: str = "") -> str:
    """Decide o tipo do erro pelo status HTTP (quando houver) e pelo texto."""
    msg = mensagem or ""
    if status == 529 or status in (500, 502, 503, 504):
        return TRANSITORIO
    if status == 429:
        return QUOTA if _QUOTA.search(msg) else TRANSITORIO
    if status in (401, 403):
        return AUTENTICACAO
    if status in (400, 404, 422):
        return CONFIGURACAO
    if _QUOTA.search(msg):
        return QUOTA
    if _AUTENTICACAO.search(msg):
        return AUTENTICACAO
    if _CONFIGURACAO.search(msg):
        return CONFIGURACAO
    if _REDE.search(msg):
        return REDE
    if re.search(r"overloaded|529|too many requests|rate.?limit", msg, re.IGNORECASE):
        return TRANSITORIO
    return DESCONHECIDO


def fatal(tipo: str | None) -> bool:
    """Erros que nunca acionam fallback: a causa precisa da pessoa."""
    return tipo in (AUTENTICACAO, REDE, CONFIGURACAO)
