// Thin wrapper around the kubectl binary. Every tool goes through here, so
// namespace checks, timeouts and cancellation live in one place.

import { execFile } from 'node:child_process'

export interface KubectlOptions {
  kubectlPath: string
  context?: string
  allowedNamespaces: readonly string[]
  timeoutMs: number
}

/**
 * A failure the model should see and react to. dsh turns any thrown error into
 * an `isError` tool result, so the message is written for the model.
 */
export class KubectlError extends Error {
  constructor(message: string, readonly code: 'NAMESPACE_NOT_ALLOWED' | 'KUBECTL_FAILED') {
    super(message)
    this.name = 'KubectlError'
  }
}

export function assertNamespace(options: KubectlOptions, namespace: string): void {
  if (!options.allowedNamespaces.includes(namespace)) {
    throw new KubectlError(
      `Namespace "${namespace}" is not allowed. Allowed namespaces: ${options.allowedNamespaces.join(', ')}.`,
      'NAMESPACE_NOT_ALLOWED',
    )
  }
}

/** Run kubectl with `args`; resolves stdout, rejects with KubectlError on a non-zero exit. */
export function kubectl(options: KubectlOptions, namespace: string, args: string[], signal: AbortSignal): Promise<string> {
  assertNamespace(options, namespace)
  const fullArgs = [...(options.context ? ['--context', options.context] : []), '-n', namespace, ...args]
  return new Promise((resolve, reject) => {
    execFile(options.kubectlPath, fullArgs, {
      signal,                       // honour cancellation: the user pressed stop
      timeout: options.timeoutMs,
      maxBuffer: 256 * 1024 * 1024, // logs can be large; the spill policy trims what the model sees
      encoding: 'utf8',
    }, (error, stdout, stderr) => {
      if (error) {
        // Let cancellation propagate untouched so dsh records it as ABORTED.
        if (error.name === 'AbortError') return reject(error)
        const detail = (stderr || error.message).trim()
        return reject(new KubectlError(`kubectl ${args.join(' ')} failed: ${detail}`, 'KUBECTL_FAILED'))
      }
      resolve(stdout)
    })
  })
}
