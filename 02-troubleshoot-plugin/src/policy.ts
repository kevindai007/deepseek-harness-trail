// Q7: the hard boundary lives outside the model.
//
// A `tools/pre-execute` listener sees every tool call before it runs, including
// calls to the built-in `bash` tool. It can let the call continue (`next()`),
// ask a human (`ask`), or refuse (`deny`). The model cannot talk its way past
// this code, no matter what a log line tells it to do.

import type { Context } from '@deepseek-ai/cordis'
import Schema from '@deepseek-ai/schemastery'
import type { PreToolDecision, ToolExecution } from '@deepseek-ai/dsh-tools'

export const name = 'trail-k8s-policy'
export const inject = ['tools']

export interface Config {
  /** Our own tools that change the cluster: always ask a human. */
  askTools: string[]
  /** Namespaces any kubectl call may touch. Everything else is denied. */
  allowedNamespaces: string[]
}

export const Config: Schema<Config> = Schema.object({
  askTools: Schema.array(Schema.string()).default(['k8s_restart_deployment']),
  allowedNamespaces: Schema.array(Schema.string()).default(['trail-dev']),
})

// kubectl verbs that change cluster state when run through bash.
const KUBECTL_WRITE = /\bkubectl\b[^|;&]*\b(delete|apply|create|replace|patch|edit|scale|rollout\s+(restart|undo)|set|drain|cordon|taint|exec|cp)\b/
const KUBECTL_DELETE = /\bkubectl\b[^|;&]*\bdelete\b/
const NAMESPACE_FLAG = /(?:-n|--namespace)[=\s]+([\w-]+)/g

function namespacesIn(command: string): string[] {
  return [...command.matchAll(NAMESPACE_FLAG)].map(m => m[1])
}

function decideBash(exec: ToolExecution, config: Config): PreToolDecision | undefined {
  const command = String((exec.arguments as { command?: unknown } | null)?.command ?? '')
  if (!/\bkubectl\b/.test(command)) return undefined
  const outside = namespacesIn(command).filter(n => !config.allowedNamespaces.includes(n))
  if (outside.length > 0 || /\s(-A|--all-namespaces)\b/.test(command)) {
    return { kind: 'deny', reason: `kubectl outside the allowed namespaces (${config.allowedNamespaces.join(', ')}) is blocked by policy.` }
  }
  if (KUBECTL_DELETE.test(command)) {
    return { kind: 'deny', reason: 'kubectl delete is never allowed from the agent. Report the finding to the user instead.' }
  }
  if (KUBECTL_WRITE.test(command)) {
    return { kind: 'ask', reason: `This kubectl command changes the cluster: ${command}` }
  }
  return undefined
}

export function apply(ctx: Context, config: Config) {
  ctx.on('tools/pre-execute', async (exec, next): Promise<PreToolDecision> => {
    if (config.askTools.includes(exec.name)) {
      const args = exec.arguments as { deployment?: string, reason?: string }
      return { kind: 'ask', reason: `Restart ${args.deployment ?? 'a deployment'}? Agent's reason: ${args.reason ?? '(none given)'}` }
    }
    if (exec.name === 'bash') {
      const decision = decideBash(exec, config)
      if (decision) return decision
    }
    return next()
  })
}
