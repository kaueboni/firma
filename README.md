# Firma

Um time de agentes que transforma PRDs em PRs enquanto você faz outra coisa.
Roda com o Claude Agent SDK na sua assinatura Claude, sem API cobrada por token.

> Estado: esqueleto. Hoje existem `firma doctor` (com a sonda da quota), `firma init` e `firma projetos`.

## Instalação (sem PyPI)

```bash
# desenvolvendo: instala a pasta em modo editável
git clone https://github.com/kaueboni/firma.git ~/code/firma
cd ~/code/firma
uv tool install --editable .

# em outra máquina, direto do GitHub
uv tool install "git+https://github.com/kaueboni/firma"
```

No Windows (PowerShell), os mesmos comandos funcionam; use `$HOME\code\firma` como pasta. Se `firma` não for reconhecido depois de instalar, rode `uv tool update-shell` e abra um terminal novo.

Pré-requisitos: Python 3.10+, git, GitHub CLI (`gh auth login`) e o Claude Code logado com a sua assinatura.

## Comandos

```bash
firma --version
firma doctor            # confere Python, git, gh, SDK e credencial
firma doctor --quota    # faz uma chamada mínima e mostra o que o SDK informa sobre a quota
firma init              # dentro de um repositório: detecta, mostra, confirma e cria .firma/
firma projetos          # lista os projetos registrados nesta máquina
```

`firma init` detecta a branch principal, o repositório no GitHub e os comandos de testes e lint (Python, Node, Go, Rust ou Makefile), na raiz e em cada subpasta que seja um projeto (ex.: `agent/`, `web/`). Depois cria `.firma/` com `config.toml`, `backlog/` e `INBOX.md`, acrescenta `.firma/` ao `.gitignore` e registra o projeto em `~/.firma/projetos.json`. Não altera o código, o `CLAUDE.md` nem `.claude/`. O registro é único por máquina porque a quota é da conta: a Firma divide o mesmo orçamento entre todos os projetos.

`firma doctor` falha se encontrar `ANTHROPIC_API_KEY` no ambiente: com ela, o Claude Code cobra por token em vez de usar a assinatura.

A sonda da quota responde ao primeiro risco técnico: se o SDK informar a utilização da quota em `rate_limit_event`, o orçamento semanal pode ser calculado direto; se não, a Firma estima pelos tokens. A resposta completa fica em `~/.firma/sondas/`.

## Desenvolvimento

```bash
uv sync
uv run pytest
```

## Para onde vai

PRD (com um agente que ajuda a escrever) → fila → spec, plano e código com tech-lead, developer, auditor e advisor → PR em rascunho no GitHub. Correções pedidas com comentários `/firma` no PR. Um governador de quota gasta no máximo uma fração do limite semanal e retoma sozinho quando o limite volta.
