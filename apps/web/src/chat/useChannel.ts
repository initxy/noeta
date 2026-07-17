import { useCallback, useEffect, useReducer, useRef } from 'react'
import { channelStreamUrl } from '../api/endpoints'
import { readSSE } from '../api/sse'
import type { ChannelMessage, ChannelTopic } from '../api/types'

/**
 * Channel SSE hook (ADR-0016): message frames are appended with seq-based dedup;
 * topic_update / topics_snapshot merge by topic.id (overwrite). Disconnects resume
 * from lastSeq (replay loses nothing and repeats nothing); the reconnect backoff
 * matches useChat.
 */

interface ChannelState {
  messages: ChannelMessage[]
  /** topic_id → topic (data source for the cards) */
  topics: Record<string, ChannelTopic>
  /** Initial replay finished (replay_done received). */
  connected: boolean
  /** Whether the physical SSE connection is up. */
  sockOpen: boolean
  lastSeq: number
}

const initialState: ChannelState = {
  messages: [],
  topics: {},
  connected: false,
  sockOpen: false,
  lastSeq: 0,
}

type Action =
  | { type: 'reset' }
  | { type: 'sock_open' }
  | { type: 'sock_closed' }
  | { type: 'frame'; event: string; data: unknown }
  | { type: 'prepend'; messages: ChannelMessage[] }

function reducer(state: ChannelState, action: Action): ChannelState {
  switch (action.type) {
    case 'reset':
      return initialState
    case 'sock_open':
      return { ...state, sockOpen: true }
    case 'sock_closed':
      return { ...state, sockOpen: false }
    case 'prepend': {
      const known = new Set(state.messages.map((m) => m.seq))
      const add = action.messages.filter((m) => !known.has(m.seq))
      if (add.length === 0) return state
      return { ...state, messages: [...add, ...state.messages] }
    }
    case 'frame': {
      if (action.event === 'message') {
        const msg = action.data as ChannelMessage
        if (state.messages.some((m) => m.seq === msg.seq)) {
          // Already present (preloaded via history pagination): it may still carry a
          // new topic_id marker, so update in place.
          return {
            ...state,
            messages: state.messages.map((m) => (m.seq === msg.seq ? msg : m)),
            lastSeq: Math.max(state.lastSeq, msg.seq),
          }
        }
        return {
          ...state,
          messages: [...state.messages, msg],
          lastSeq: Math.max(state.lastSeq, msg.seq),
        }
      }
      if (action.event === 'topic_update') {
        const topic = action.data as ChannelTopic
        return { ...state, topics: { ...state.topics, [topic.id]: topic } }
      }
      if (action.event === 'topics_snapshot') {
        const { topics } = action.data as { topics: ChannelTopic[] }
        const map = { ...state.topics }
        for (const t of topics) map[t.id] = t
        return { ...state, topics: map }
      }
      if (action.event === 'replay_done') {
        return { ...state, connected: true }
      }
      return state
    }
  }
}

export function useChannel(channelId: string | null) {
  const [state, dispatch] = useReducer(reducer, initialState)
  const lastSeqRef = useRef(0)
  lastSeqRef.current = state.lastSeq

  useEffect(() => {
    dispatch({ type: 'reset' })
    lastSeqRef.current = 0
    if (!channelId) return

    const controller = new AbortController()
    let retryTimer: number | undefined
    let attempt = 0

    const connect = () => {
      readSSE(
        channelStreamUrl(channelId, lastSeqRef.current),
        controller.signal,
        (frame) => {
          dispatch({
            type: 'frame',
            event: frame.event.type,
            data: frame.event.data,
          })
        },
        () => {
          attempt = 0
          dispatch({ type: 'sock_open' })
        },
      )
        .then(() => {
          if (controller.signal.aborted) return
          dispatch({ type: 'sock_closed' })
          scheduleRetry()
        })
        .catch(() => {
          if (controller.signal.aborted) return
          dispatch({ type: 'sock_closed' })
          scheduleRetry()
        })
    }

    const scheduleRetry = () => {
      if (controller.signal.aborted) return
      attempt += 1
      const delay = Math.min(1000 * 2 ** Math.min(attempt - 1, 3), 8000)
      retryTimer = window.setTimeout(connect, delay)
    }

    connect()
    return () => {
      controller.abort()
      if (retryTimer !== undefined) window.clearTimeout(retryTimer)
    }
  }, [channelId])

  /** History pagination preload (merge channelsApi.messages results at the head). */
  const prependHistory = useCallback((messages: ChannelMessage[]) => {
    dispatch({ type: 'prepend', messages })
  }, [])

  return { ...state, prependHistory }
}
