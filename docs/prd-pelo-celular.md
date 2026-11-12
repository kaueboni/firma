# Rascunho de PRD pelo celular

O `/prd` no Claude Code é o caminho completo, porque lê o código antes de perguntar. Pelo celular dá para adiantar o rascunho em qualquer lugar e terminar depois no computador.

## Configurar uma vez

1. No claude.ai, crie um projeto (por exemplo "PRDs da Firma").
2. Nas instruções do projeto, cole:

```text
Você me ajuda a escrever PRDs para a Firma, um time de agentes que implementa sozinho a partir deles.
Faça UMA pergunta por mensagem. Descubra o tipo (feature, bug ou refatoracao), o problema,
o comportamento esperado, critérios de aceite verificáveis por teste ou comando, o que fica fora
de escopo e o que o advisor NÃO pode decidir sozinho. Não invente requisitos: o que eu não souber
vira "Perguntas em aberto". No fim, entregue o PRD no modelo abaixo, num bloco de código.
```

3. Ainda no projeto, adicione como arquivo o modelo `src/firma/modelos/prd.md` deste repositório.

## Usar

- Converse até o PRD sair no bloco de código. Ele não vai conhecer o seu código, então deixe as dúvidas técnicas em "Perguntas em aberto".
- No computador, dentro do projeto, rode `/prd` no Claude Code e cole o rascunho como argumento. O agente lê o código, resolve as perguntas com você, cria o arquivo em `.firma/backlog/` e põe na fila.

Quando existir o adaptador do Notion, o celular vai poder escrever direto na fila e este passo de colar some.
