# Firma

Um time de agentes que transforma PRDs em PRs enquanto você faz outra coisa.
Roda com o Claude Agent SDK na sua assinatura Claude, sem API cobrada por token.

> Estado: esqueleto. Hoje só existem `firma doctor` e a sonda da quota.

## Instalação (sem PyPI)

```bash
# desenvolvendo: instala a pasta em modo editável
git clone git@github.com:kaueboni/firma.git ~/code/firma
cd ~/code/firma
uv tool install --editable .

# em outra máquina, direto do GitHub
uv tool install git+ssh://git@github.com/kaueboni/firma
```

Pré-requisitos: Python 3.10+, git, GitHub CLI (`gh auth login`) e o Claude Code logado com a sua assinatura.

## Comandos

```bash
firma --version
firma doctor            # confere Python, git, gh, SDK e credencial
firma doctor --quota    # faz uma chamada mínima e mostra o que o SDK informa sobre a quota
```

`firma doctor` falha se encontrar `ANTHROPIC_API_KEY` no ambiente: com ela, o Claude Code cobra por token em vez de usar a assinatura.

A sonda da quota responde ao primeiro risco técnico: se o SDK informar a utilização da quota em `rate_limit_event`, o orçamento semanal pode ser calculado direto; se não, a Firma estima pelos tokens. A resposta completa fica em `~/.firma/sondas/`.

## Desenvolvimento

```bash
uv sync
uv run pytest
```

## Para onde vai

PRD (com um agente que ajuda a escrever) → fila → spec, plano e código com tech-lead, developer, auditor e advisor → PR em rascunho no GitHub. Correções pedidas com comentários `/firma` no PR. Um governador de quota gasta no máximo uma fração do limite semanal e retoma sozinho quando o limite volta.
