# Firma

Um time de agentes que transforma PRDs em PRs enquanto você faz outra coisa.
Roda o Claude pela sua assinatura (Claude Agent SDK, sem API cobrada por token) e o Gemini pelo OpenCode,
por papel ou como fallback quando a quota do Claude acaba.

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

Pré-requisitos: Python 3.10+, git, GitHub CLI (`gh auth login`), o Claude Code logado com a sua assinatura e o OpenCode 1.19+ com o Gemini conectado:

```bash
npm install -g opencode-ai
opencode auth login      # escolha Google e cole a chave do AI Studio (fica no OpenCode, nunca na Firma)
firma doctor --opencode  # confere que o Gemini responde
```

## Comandos

```bash
firma --version
firma doctor            # confere Python, git, gh, SDK, credencial e OpenCode
firma doctor --quota    # faz uma chamada mínima e mostra o que o SDK informa sobre a quota
firma doctor --opencode # sobe o OpenCode e faz uma chamada mínima em cada modelo não-Claude do projeto
firma init              # dentro de um repositório: detecta, mostra, confirma e cria .firma/
firma projetos          # lista os projetos registrados nesta máquina
firma claude instalar   # instala o /prd no Claude Code (uma vez por máquina)
firma prd novo "Título" --tipo feature   # cria um PRD a partir do modelo (feature, bug, refatoracao)
firma prd listar        # backlog com status
firma prd checar PRD-001   # confere a regra de pronto
firma prd pronto PRD-001   # se passar, põe na fila
```

`firma init` detecta a branch principal, o repositório no GitHub e os comandos de testes e lint (Python, Node, Go, Rust ou Makefile), na raiz e em cada subpasta que seja um projeto (ex.: `agent/`, `web/`). Depois cria `.firma/` com `config.toml`, `backlog/` e `INBOX.md`, acrescenta `.firma/` ao `.gitignore` e registra o projeto em `~/.firma/projetos.json`. Não altera o código, o `CLAUDE.md` nem `.claude/`. Também mostra o que achou de instruções (`CLAUDE.md`, `AGENTS.md`) e skills (`.claude/skills`, `.opencode/skills`). O registro é único por máquina porque a quota é da conta: a Firma divide o mesmo orçamento entre todos os projetos.

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

1. cria a worktree `.firma/worktrees/PRD-001` na branch `firma/PRD-001` e liga a ela os `.venv`, `node_modules`, `.claude` e `.opencode` do repositório (sem reinstalar nada; as skills locais chegam aos agentes e nunca entram em commit);
2. o tech-lead escreve `docs/firma/PRD-001/spec.md` e `plano.md`, e o advisor aprova ou pede uma correção; depois da correção o advisor revisa de novo, e uma segunda reprovação estaciona o PRD;
3. commit, push e PR em rascunho, já no início, para acompanhar pelo GitHub;
4. para cada tarefa: o developer implementa com TDD, a própria Firma roda os testes e o lint do `config.toml`, o auditor revisa; reprovou, volta ao developer (até 3 rodadas, depois uma última com o diagnóstico do advisor); aprovou, commit e push;
5. no fim, o PRD vira `pr-aberto`. Se algo só a pessoa pode decidir, vira `estacionado` com o motivo em `.firma/INBOX.md`.

Regras em código, para todos os agentes (hook no Claude, plugin no OpenCode): nada de push, commit, troca de branch, `rm -rf`, `gh`, dependência nova, leitura de `.env` ou das credenciais dos agentes; escrita só dentro da worktree e nunca em `.git`, `.claude` ou `.opencode`. O tech-lead só escreve em `docs/firma/<PRD>/`; o advisor e o auditor só leem (o que mexerem é desfeito); o developer não mexe na spec.

Quota e fallback: quando o Claude fica sem quota (ou passa do `teto_semanal`), cada papel com `fallback_*` passa para o Gemini pelo resto do turno, recebendo a worktree como está e um handoff da Firma (`.firma/estado/<PRD>-handoff.md`). Sem fallback, a Firma pausa e o PRD volta para a fila. Erros de credencial, rede ou modelo inexistente nunca acionam fallback: o PRD estaciona com a causa. A Firma também para fora da `janela` ou com `firma parar`, e retoma exatamente da rodada em que parou.

Ajustes opcionais no `.firma/config.toml`:

```toml
[execucao]
permissao = "bypassPermissions"   # sessões do Claude; as regras da Firma valem em qualquer modo
rodadas = 3

# Um bloco por papel: tech_lead, advisor, developer, auditor.
# provider: anthropic (assinatura Claude) ou google (Gemini via OpenCode). Nunca ponha chaves aqui.
[modelos.developer]
provider = "anthropic"
model = ""                        # vazio = o padrão do Claude Code
fallback_provider = "google"
fallback_model = "gemini-2.5-pro"
```

O formato antigo (`[modelos]` com `trabalho` e `advisor`) continua funcionando: vira Claude com fallback para o Gemini.

Skills do projeto: prefira `.claude/skills/`, que o Claude e o OpenCode leem (`.opencode/skills/` só vale para os papéis no Gemini). Elas não precisam estar no git. Os agentes só enxergam as skills do projeto, nunca as globais da sua máquina.

## Desenvolvimento

```bash
uv sync
uv run pytest
```

## Para onde vai

PRD (com um agente que ajuda a escrever) → fila → spec, plano e código com tech-lead, developer, auditor e advisor → PR em rascunho no GitHub. Correções pedidas com comentários `/firma` no PR. Um governador de quota gasta no máximo uma fração do limite semanal e retoma sozinho quando o limite volta.
