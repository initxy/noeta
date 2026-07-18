import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from '../api/client'
import { channelsApi } from '../api/endpoints'
import type { AnswerPayload, Channel, ChannelTopic } from '../api/types'
import { useChat } from '../chat/useChat'
import { renderBlocks } from '../chat/streaming'
import { cn } from '../lib/cn'
import { useToast } from '../state/toast'
import { Conversation } from './Conversation'
import { IconClose, IconSend } from './icons'

/**
 * Topic panel (ADR-0016 D4): the right rail opened from a topic card — the agent's
 * full conversation stream (tool calls / citations / file outputs render exactly as
 * in a single-user session).
 *
 * Data plane: the channel session's existing session SSE (per-task filtering,
 * `useChat(session, task)` reused as-is); sending a message inside a topic =
 * mention-free follow-up on the original task (channelsApi.topicMessage), and
 * answering follow-up questions goes through channelsApi.topicAnswer (per-task
 * semantics — the channel session is not a workflow, so it cannot use
 * sessions/answer's session-level status check).
 */
export function TopicPanel({
  channel,
  topic,
  spaceId,
  userAvatar,
  userName,
  onClose,
}: {
  channel: Channel
  topic: ChannelTopic
  spaceId: string | null
  userAvatar?: string
  userName?: string
  onClose: () => void
}) {
  const { toast } = useToast()
  // task_id may not be persisted yet (seed in flight / sandbox cold start): poll
  // until it lands before connecting the per-task stream — an unfiltered
  // subscription would mix in the whole channel session's events.
  const [taskId, setTaskId] = useState(topic.task_id)
  useEffect(() => {
    if (taskId) return
    const timer = window.setInterval(() => {
      channelsApi
        .messages(channel.id, undefined, 1)
        .then((r) => {
          const fresh = r.topics.find((t) => t.id === topic.id)
          if (fresh?.task_id) setTaskId(fresh.task_id)
        })
        .catch(() => {})
    }, 1500)
    return () => window.clearInterval(timer)
  }, [taskId, channel.id, topic.id])

  const chat = useChat(taskId ? channel.session_id : null, taskId)
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)

  const send = useCallback(async () => {
    const content = text.trim()
    if (!content || sending) return
    setSending(true)
    try {
      await channelsApi.topicMessage(channel.id, topic.id, content)
      chat.optimisticSend(content)
      setText('')
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        toast(e.message, 'info')
      } else {
        toast(e instanceof Error ? e.message : 'Failed to send')
      }
    } finally {
      setSending(false)
      inputRef.current?.focus()
    }
  }, [channel.id, topic.id, text, sending, chat, toast])

  const answer = useCallback(
    async (questionId: string, answers: AnswerPayload) => {
      await channelsApi.topicAnswer(channel.id, topic.id, questionId, answers)
      chat.markAnswered(questionId)
    },
    [channel.id, topic.id, chat],
  )

  return (
    <aside className="flex h-full w-full flex-col border-l border-border bg-bg lg:w-[30rem] lg:shrink-0">
      {/* Header */}
      <div className="flex h-[52px] shrink-0 items-center gap-2 border-b border-border px-3">
        <span className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-ink-3">
          Topic
        </span>
        <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-ink">
          #{channel.name}
        </span>
        <button
          type="button"
          title="Close topic"
          onClick={onClose}
          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-ink-2 transition-colors hover:bg-surface-2 hover:text-ink"
        >
          <IconClose />
        </button>
      </div>

      {/* Conversation stream: reuses session rendering (tool calls / citations / question cards). */}
      <Conversation
        key={`${channel.id}:${topic.id}`}
        items={chat.items}
        running={chat.running}
        connected={chat.connected}
        connectionError={chat.connectionError}
        spaceId={spaceId}
        userAvatar={userAvatar}
        userName={userName}
        streamingBlocks={renderBlocks(chat.streaming)}
        onAnswer={answer}
        onOpenDoc={() => {}}
        workspaceFiles={[]}
        onOpenFile={() => {}}
      />

      {/* Follow-up inside the topic (no mention needed). */}
      <div className="shrink-0 border-t border-border px-3 py-3">
        <div
          className={cn(
            'flex items-end gap-2 rounded-xl border border-border bg-surface px-3 py-2',
          )}
        >
          <textarea
            ref={inputRef}
            value={text}
            rows={1}
            placeholder={chat.running ? 'The agent is working…' : 'Follow up in this topic (no @ needed)'}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault()
                void send()
              }
            }}
            className="max-h-32 min-h-[24px] flex-1 resize-none bg-transparent text-[13px] leading-6 text-ink outline-none placeholder:text-ink-3"
          />
          <button
            type="button"
            onClick={() => void send()}
            disabled={!text.trim() || sending}
            title="Send"
            className="mb-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-accent text-white transition-opacity disabled:opacity-40"
          >
            <IconSend className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
    </aside>
  )
}
