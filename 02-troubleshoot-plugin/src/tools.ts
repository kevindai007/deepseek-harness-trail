// Q5: tools designed for the model, instead of one `run_command`.
//
// Design rules shown in the talk:
//   1. One tool per question an SRE asks, with typed parameters (no free-form shell).
//   2. Return a structured value; `render` decides what text the model reads.
//      PTC programs (Q11) get the value itself.
//   3. Filter on the server side: search_logs returns matches, not 150k lines.
//   4. Read tools and write tools are separate, so policy (Q7) can tell them apart.

import type { Context } from '@deepseek-ai/cordis'
import Schema from '@deepseek-ai/schemastery'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { KubectlError, kubectl, type KubectlOptions } from './kubectl.ts'

export const name = 'trail-k8s-tools'
export const inject = ['tools']

export interface Config {
  kubectlPath: string
  context?: string
  defaultNamespace: string
  allowedNamespaces: string[]
  maxSearchMatches: number
  timeoutMs: number
}

export const Config: Schema<Config> = Schema.object({
  kubectlPath: Schema.string().default('kubectl'),
  context: Schema.string(),
  defaultNamespace: Schema.string().default('trail-dev'),
  allowedNamespaces: Schema.array(Schema.string()).default(['trail-dev']),
  maxSearchMatches: Schema.natural().min(1).default(50),
  timeoutMs: Schema.natural().min(1000).default(30000),
})

const NAMESPACE_PARAM = { type: 'string', description: 'Kubernetes namespace. Omit to use the default namespace.' } as const
const TARGET_PARAM = {
  type: 'string',
  required: true,
  description: 'A pod name (e.g. order-service-5d8b7-k2m4p) or deployment/<name> to read one pod of that deployment.',
} as const

interface PodSummary {
  name: string
  app: string
  phase: string
  status: string
  ready: boolean
  restarts: number
}

export function apply(ctx: Context, config: Config) {
  const options: KubectlOptions = {
    kubectlPath: config.kubectlPath,
    context: config.context,
    allowedNamespaces: config.allowedNamespaces,
    timeoutMs: config.timeoutMs,
  }
  const ns = (namespace?: string) => namespace?.trim() || config.defaultNamespace

  ctx.tools.register(defineTool({
    name: 'k8s_list_pods',
    description: 'List pods with their status, readiness and restart count. Start here to see which service is unhealthy.',
    parameters: {
      namespace: NAMESPACE_PARAM,
      app: { type: 'string', description: 'Only pods with label app=<app>, e.g. order-service.' },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          namespace: { type: 'string', required: true },
          pods: {
            type: 'array',
            required: true,
            items: {
              type: 'object',
              additionalProperties: false,
              properties: {
                name: { type: 'string', required: true },
                app: { type: 'string', required: true },
                phase: { type: 'string', required: true },
                status: { type: 'string', required: true },
                ready: { type: 'boolean', required: true },
                restarts: { type: 'integer', required: true },
              },
            },
          },
        },
      },
      render: (_args, value) => [{
        type: 'text',
        text: value.pods.length === 0
          ? `No pods found in ${value.namespace}.`
          : value.pods.map(p => `${p.name}  app=${p.app}  status=${p.status}  ready=${p.ready}  restarts=${p.restarts}`).join('\n'),
      }],
    },
    async execute(args, exec) {
      const namespace = ns(args.namespace)
      const raw = await kubectl(options, namespace, ['get', 'pods', ...(args.app ? ['-l', `app=${args.app}`] : []), '-o', 'json'], exec.signal)
      const items = (JSON.parse(raw).items ?? []) as any[]
      const pods: PodSummary[] = items.map(item => {
        const cs = item.status?.containerStatuses?.[0] ?? {}
        return {
          name: item.metadata.name,
          app: item.metadata.labels?.app ?? '',
          phase: item.status?.phase ?? 'Unknown',
          status: cs.state?.waiting?.reason ?? item.status?.phase ?? 'Unknown',
          ready: Boolean(cs.ready),
          restarts: cs.restartCount ?? 0,
        }
      })
      return { namespace, pods }
    },
  }))

  ctx.tools.register(defineTool({
    name: 'k8s_describe_pod',
    description: 'Show a pod\'s container state, last termination reason (e.g. OOMKilled), resource limits and environment.',
    parameters: { pod: { type: 'string', required: true, description: 'Exact pod name.' }, namespace: NAMESPACE_PARAM },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    async execute(args, exec) {
      return kubectl(options, ns(args.namespace), ['describe', 'pod', args.pod], exec.signal)
    },
  }))

  ctx.tools.register(defineTool({
    name: 'k8s_get_events',
    description: 'List recent Kubernetes events (rollouts, back-offs, OOM kills), oldest first.',
    parameters: {
      namespace: NAMESPACE_PARAM,
      warningsOnly: { type: 'boolean', description: 'Only Warning events. Defaults to false.' },
    },
    output: {
      schema: {
        type: 'array',
        items: {
          type: 'object',
          additionalProperties: false,
          properties: {
            time: { type: 'string', required: true },
            type: { type: 'string', required: true },
            reason: { type: 'string', required: true },
            object: { type: 'string', required: true },
            message: { type: 'string', required: true },
          },
        },
      },
      render: (_args, value) => [{
        type: 'text',
        text: value.length === 0 ? 'No events.' : value.map(e => `${e.time} ${e.type} ${e.reason} ${e.object}: ${e.message}`).join('\n'),
      }],
    },
    async execute(args, exec) {
      const raw = await kubectl(options, ns(args.namespace), ['get', 'events', '-o', 'json'], exec.signal)
      const events = ((JSON.parse(raw).items ?? []) as any[]).map(e => ({
        time: String(e.lastTimestamp ?? ''),
        type: String(e.type ?? ''),
        reason: String(e.reason ?? ''),
        object: `${String(e.involvedObject?.kind ?? '').toLowerCase()}/${String(e.involvedObject?.name ?? '')}`,
        message: String(e.message ?? ''),
      }))
      return args.warningsOnly ? events.filter(e => e.type === 'Warning') : events
    },
  }))

  ctx.tools.register(defineTool({
    name: 'k8s_search_logs',
    description: 'Search a pod\'s logs with a regular expression and return only matching lines. '
      + 'Prefer this over k8s_fetch_logs: it scans the whole log on the server and returns at most a few dozen lines.',
    parameters: {
      target: TARGET_PARAM,
      pattern: { type: 'string', required: true, description: 'JavaScript regular expression, case-insensitive, e.g. "ERROR|Exception|timed out".' },
      sinceMinutes: { type: 'integer', description: 'Only lines from the last N minutes.' },
      previous: { type: 'boolean', description: 'Read the previous (crashed) container instead of the current one.' },
      namespace: NAMESPACE_PARAM,
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          target: { type: 'string', required: true },
          scannedLines: { type: 'integer', required: true },
          matchCount: { type: 'integer', required: true },
          matches: {
            type: 'array',
            required: true,
            items: {
              type: 'object',
              additionalProperties: false,
              properties: { line: { type: 'integer', required: true }, text: { type: 'string', required: true } },
            },
          },
          truncated: { type: 'boolean', required: true },
        },
      },
      render: (_args, value) => [{
        type: 'text',
        text: `Scanned ${value.scannedLines} lines of ${value.target}; ${value.matchCount} matched`
          + (value.truncated ? ` (showing the last ${value.matches.length})` : '')
          + (value.matches.length ? ':\n' + value.matches.map(m => `#${m.line} ${m.text}`).join('\n') : '.'),
      }],
    },
    async execute(args, exec) {
      let regex: RegExp
      try {
        regex = new RegExp(args.pattern, 'i')
      } catch (error) {
        throw new KubectlError(`Invalid regular expression: ${(error as Error).message}`, 'KUBECTL_FAILED')
      }
      const kargs = ['logs', args.target]
      if (args.sinceMinutes) kargs.push(`--since=${args.sinceMinutes}m`)
      if (args.previous) kargs.push('--previous')
      const lines = (await kubectl(options, ns(args.namespace), kargs, exec.signal)).split('\n').filter(Boolean)
      const all = lines.flatMap((text, i) => (regex.test(text) ? [{ line: i + 1, text }] : []))
      const matches = all.slice(-config.maxSearchMatches)
      return { target: args.target, scannedLines: lines.length, matchCount: all.length, matches, truncated: all.length > matches.length }
    },
  }))

  ctx.tools.register(defineTool({
    name: 'k8s_fetch_logs',
    description: 'Return raw log lines from a pod. Large outputs are saved to a file and only a preview is shown; '
      + 'use k8s_search_logs to find specific errors.',
    parameters: {
      target: TARGET_PARAM,
      tail: { type: 'integer', description: 'Only the last N lines. Omit for the whole log.' },
      previous: { type: 'boolean', description: 'Read the previous (crashed) container.' },
      namespace: NAMESPACE_PARAM,
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    async execute(args, exec) {
      const kargs = ['logs', args.target]
      if (args.tail !== undefined) kargs.push(`--tail=${args.tail}`)
      if (args.previous) kargs.push('--previous')
      return kubectl(options, ns(args.namespace), kargs, exec.signal)
    },
  }))

  ctx.tools.register(defineTool({
    name: 'k8s_restart_deployment',
    description: 'Restart all pods of a deployment (kubectl rollout restart). This changes the cluster; '
      + 'only use it when the evidence shows a restart will help, and state why in `reason`.',
    parameters: {
      deployment: { type: 'string', required: true, description: 'Deployment name, e.g. payment-service.' },
      reason: { type: 'string', required: true, description: 'Why a restart is the right fix, citing the evidence.' },
      namespace: NAMESPACE_PARAM,
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    async execute(args, exec) {
      return kubectl(options, ns(args.namespace), ['rollout', 'restart', `deployment/${args.deployment}`], exec.signal)
    },
  }))
}
