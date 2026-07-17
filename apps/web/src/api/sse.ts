import type { SSEFrame, UIEvent } from './types'

/**
 * Hand-rolled SSE reader: fetch + ReadableStream.
 * Frame format (matching the backend contract): `id: <seq>` / `event: <type>` / `data: <json>`,
 * blank line between frames; comment lines starting with `:` (heartbeats) are ignored.
 */
export async function readSSE(
  url: string,
  signal: AbortSignal,
  onFrame: (frame: SSEFrame) => void,
  onOpen?: () => void,
): Promise<void> {
  const res = await fetch(url, {
    credentials: 'include',
    headers: { Accept: 'text/event-stream' },
    signal,
  })
  if (!res.ok || !res.body) {
    throw new Error(`SSE connection failed (${res.status})`)
  }
  onOpen?.()

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  const flushBlock = (block: string) => {
    let seq: number | null = null
    let eventType = ''
    const dataLines: string[] = []
    for (const rawLine of block.split('\n')) {
      const line = rawLine.replace(/\r$/, '')
      if (!line || line.startsWith(':')) continue
      if (line.startsWith('id:')) seq = Number(line.slice(3).trim())
      else if (line.startsWith('event:')) eventType = line.slice(6).trim()
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
    }
    // No id line = synthetic event (replay_done, drive-failure error, etc.); deliver with seq null.
    if (!eventType || dataLines.length === 0) return
    try {
      const data = JSON.parse(dataLines.join('\n'))
      onFrame({ seq, event: { type: eventType, data } as UIEvent })
    } catch {
      /* Skip unparseable frames and keep the stream going. */
    }
  }

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    for (;;) {
      const idx = buffer.indexOf('\n\n')
      if (idx === -1) break
      const block = buffer.slice(0, idx)
      buffer = buffer.slice(idx + 2)
      flushBlock(block)
    }
  }
  if (buffer.trim()) flushBlock(buffer)
}
