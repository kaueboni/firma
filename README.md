# Firma

Um time de agentes que transforma PRDs em PRs enquanto você faz outra coisa.
Roda com o Claude Agent SDK na sua assinatura Claude, sem API cobrada por token.

> Estado: em construção. Já dá para rodar: `firma run` leva PRDs prontos até um PR em rascunho. Ainda não há agendamento, comentários `/firma` nem espera pela volta da quota.

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
firma claude instalar   # instala o /prd no Claude Code (uma vez por máquina)
firma prd novo "Título" --tipo feature   # cria um PRD a partir do modelo (feature, bug, refatoracao)
firma prd listar        # backlog com status
firma prd checar PRD-001   # confere a regra de pronto
firma prd pronto PRD-001   # se passar, põe na fila
```

`firma init` detecta a branch principal, o repositório no GitHub e os comandos de testes e lint (Python, Node, Go, Rust ou Makefile), na raiz e em cada subpasta que seja um projeto (ex.: `agent/`, `web/`). Depois cria `.firma/` com `config.toml`, `backlog/` e `INBOX.md`, acrescenta `.firma/` ao `.gitignore` e registra o projeto em `~/.firma/projetos.json`. Não altera o código, o `CLAUDE.md` nem `.claude/`. O registro é único por máquina porque a quota é da conta: a Firma divide o mesmo orçamento entre todos os projetos.

`firma doctor` falha se encontrar `ANTHROPIC_API_KEY` no ambiente: com ela, o Claude Code cobra por token em vez de usar a assinatura.

A sonda da quota responde ao primeiro risco técnico: se o SDK informar a utilização da quota em `rate_limit_event`, o orçamento semanal pode ser calculado direto; se não, a Firma estima pelos tokens. A resposta completa fica em `~/.firma/sondas/`.

## PRDs

O PRD é o único ponto em que você decide: a Firma escreve a spec e o advisor aprova. Por isso ele só entra na fila se passar na regra de pronto: tipo, tamanho (P ou M; G precisa ser quebrado), prioridade, todas as seções preenchidas (sem `[PREENCHER]`), critérios de aceite em lista e nenhuma pergunta em aberto. Critérios vagos ("fica melhor") geram aviso.

O jeito mais fácil de escrever um é o `/prd <ideia>` no Claude Code: ele lê o código, pergunta uma coisa por vez e roda `firma prd checar` até passar. Para adiantar pelo celular, veja [docs/prd-pelo-celular.md](docs/prd-pelo-celular.md).

## Rodar

```bash
firma run                       # todos os projetos registrados, até a fila acabar ou um limite parar
firma run -p meu-projeto --max 1   # bom para o primeiro teste
firma assistir                  # em outro terminal: o feed ao vivo
firma status                    # turno, quota da semana e fila de cada projeto
firma parar                     # para depois da sessão atual, sem perder trabalho
```

Para cada PRD pronto, a Firma:

1. cria a worktree `.firma/worktrees/PRD-001` na branch `firma/PRD-001` e liga a ela os `.venv` e `node_modules` do repositório (sem reinstalar nada);
2. o tech-lead escreve `docs/firma/PRD-001/spec.md` e `plano.md`, e o advisor (Opus) aprova ou pede uma correção;
3. commit, push e PR em rascunho, já no início, para acompanhar pelo GitHub;
4. para cada tarefa: o developer implementa com TDD, a própria Firma roda os testes e o lint do `config.toml`, o auditor revisa; reprovou, volta ao developer (até 3 rodadas, depois uma última com o diagnóstico do advisor); aprovou, commit e push;
5. no fim, o PRD vira `pr-aberto`. Se algo só a pessoa pode decidir, vira `estacionado` com o motivo em `.firma/INBOX.md`.

Regras em código, para todos os agentes: nada de push, commit, troca de branch, `rm -rf`, `gh`, dependência nova ou leitura de `.env`; escrita só dentro da worktree (e o tech-lead só em `docs/firma/`). A Firma para quando a quota semanal da conta passa do `teto_semanal`, fora da `janela` ou com `firma parar`, e o PRD volta para a fila e retoma de onde parou.

Ajustes opcionais no `.firma/config.toml`:

```toml
[execucao]
permissao = "bypassPermissions"   # as regras da Firma valem em qualquer modo
rodadas = 3

[modelos]
trabalho = ""      # vazio = o padrão da sua conta
advisor = "opus"
```

## Desenvolvimento

```bash
uv sync
uv run pytest
```

## Para onde vai

PRD (com um agente que ajuda a escrever) → fila → spec, plano e código com tech-lead, developer, auditor e advisor → PR em rascunho no GitHub. Correções pedidas com comentários `/firma` no PR. Um governador de quota gasta no máximo uma fração do limite semanal e retoma sozinho quando o limite volta.
