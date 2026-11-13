// Guarda da Firma dentro do OpenCode: as mesmas regras de src/firma/guarda.py, aplicadas
// Atenção: o OpenCode chama toda função exportada como plugin; exporte só o FirmaGuarda.
// antes de cada ferramenta. As regras de shell e o escopo de cada sessão vêm de um JSON que a
// Firma escreve (FIRMA_OPENCODE_ESTADO); a fonte única das regras continua sendo o guarda.py.
import { readFileSync, realpathSync } from "node:fs"
import path from "node:path"

type Escopo = {
  papel: string
  raiz: string
  pode_escrever: boolean
  escrita_so_em?: string | null
  escrita_proibida?: string[]
}
type Estado = { regras: { padrao: string; motivo: string }[]; sessoes: Record<string, Escopo> }

const ESCRITA = new Set(["edit", "write", "patch", "apply_patch", "multiedit"])
const SHELL = new Set(["bash", "shell"])
const SEMPRE_PROIBIDO = [".git", ".opencode", ".claude"]
const CREDENCIAIS = /[\/\\]\.local[\/\\]share[\/\\]opencode([\/\\]|$)|[\/\\]\.config[\/\\]opencode([\/\\]|$)|[\/\\]\.claude[\/\\]\.credentials/i

function ler(): Estado | null {
  const arquivo = process.env.FIRMA_OPENCODE_ESTADO
  if (!arquivo) return null
  try {
    return JSON.parse(readFileSync(arquivo, "utf8"))
  } catch {
    return null
  }
}

const normal = (p: string) => {
  const n = path.normalize(p)
  return process.platform === "win32" ? n.toLowerCase() : n
}

function absoluto(p: string, raiz: string) {
  return path.isAbsolute(p) ? p : path.join(raiz, p)
}

// sem seguir junções: `.claude` ligado continua sendo `.claude`
function dentroLogico(p: string, raiz: string, pasta: string) {
  const alvo = normal(absoluto(p, raiz))
  const base = normal(path.join(raiz, pasta))
  return alvo === base || alvo.startsWith(base + path.sep)
}

function real(p: string) {
  try {
    return realpathSync(p)
  } catch {
    const pai = path.dirname(p)
    return pai === p ? p : path.join(real(pai), path.basename(p))
  }
}

function dentroReal(p: string, raiz: string) {
  const alvo = normal(real(absoluto(p, raiz)))
  const base = normal(real(raiz))
  return alvo === base || alvo.startsWith(base + path.sep)
}

function eEnv(p: string) {
  const nome = path.basename(p.replace(/\\/g, "/"))
  return nome === ".env" || (nome.startsWith(".env.") && nome !== ".env.example")
}

function caminhos(ferramenta: string, args: any): string[] {
  const lista: string[] = []
  for (const campo of ["filePath", "file_path", "path"]) if (typeof args?.[campo] === "string") lista.push(args[campo])
  if (typeof args?.patchText === "string") {
    for (const m of args.patchText.matchAll(/^\*\*\* (?:Add|Update|Delete) File: (.+)$/gm)) lista.push(m[1].trim())
    for (const m of args.patchText.matchAll(/^\*\*\* Move to: (.+)$/gm)) lista.push(m[1].trim())
  }
  return lista
}

function avaliar(ferramenta: string, args: any, escopo: Escopo, regras: Estado["regras"]): string | null {
  if (SHELL.has(ferramenta)) {
    const comando = String(args?.command ?? "")
    for (const r of regras) if (new RegExp(r.padrao, "i").test(comando)) return `Bloqueado pela Firma: ${r.motivo}.`
    return null
  }
  const lista = caminhos(ferramenta, args)
  for (const p of lista) {
    if (eEnv(p)) return "Bloqueado pela Firma: arquivos .env podem ter segredos."
    if (CREDENCIAIS.test(absoluto(p, escopo.raiz))) return "Bloqueado pela Firma: credenciais dos agentes."
  }
  if (ESCRITA.has(ferramenta)) {
    if (!escopo.pode_escrever) return "Bloqueado pela Firma: este papel não edita arquivos."
    for (const p of lista) {
      for (const pasta of SEMPRE_PROIBIDO)
        if (dentroLogico(p, escopo.raiz, pasta)) return `Bloqueado pela Firma: ninguém escreve em ${pasta}/.`
      if (!dentroReal(p, escopo.raiz)) return "Bloqueado pela Firma: só é permitido escrever dentro da worktree da feature."
      if (escopo.escrita_so_em && !dentroReal(p, path.join(escopo.raiz, escopo.escrita_so_em)))
        return `Bloqueado pela Firma: este papel só escreve em ${escopo.escrita_so_em}/.`
      for (const pasta of escopo.escrita_proibida ?? [])
        if (dentroLogico(p, escopo.raiz, pasta)) return `Bloqueado pela Firma: este papel não escreve em ${pasta}/.`
    }
  }
  return null
}

export const FirmaGuarda = async () => ({
  "tool.execute.before": async (input: { tool: string; sessionID: string }, output: { args: any }) => {
    const estado = ler()
    if (!estado) return
    const escopo = estado.sessoes[input.sessionID]
    if (!escopo) return // sessão que não é da Firma
    const motivo = avaliar(input.tool, output.args, escopo, estado.regras)
    if (motivo) throw new Error(motivo)
  },
})
