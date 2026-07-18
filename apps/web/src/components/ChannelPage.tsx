import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ApiError } from '../api/client'
import { channelsApi } from '../api/endpoints'
import type { Channel, ChannelMessage, ChannelTopic } from '../api/types'
import { useChannel } from '../chat/useChannel'
import { cn } from '../lib/cn'
import { relativeTime } from '../lib/time'
import { useToast } from '../state/toast'
import { IconPlus, IconSend } from './icons'
import { Markdown } from './Markdown'

/**
 * Channel page (ADR-0016): main stream messages + topic cards + composer.
 *
 * - The main stream holds only human chat messages and topic cards; agent replies
 *   and execution live in the topic panel (onOpenTopic).
 * - @Agent comes from the composer's mention interaction (the send carries
 *   mention_agent metadata; the backend does not parse the body, D7).
 * - Unread watermark: pushed as soon as a new message lands (being in the channel
 *   = read).
 */

const MENTION_RE = /(^|\s)@Agent(\s|$|[,，。:：])/

interface ChannelPageProps {
  channel: Channel
  /** Open the topic panel (right rail). */
  onOpenTopic: (topic: ChannelTopic) => void
  /** Topic → board card (Phase 2); pass undefined to hide the entry. */
  onTopicToCard?: (topic: ChannelTopic) => void
  /** Topic to auto-open when jumping in from a board backlink. */
  focusTopicId?: string | null
  onFocusConsumed?: () => void
  currentUser?: string
}

export function ChannelPage({
  channel,
  onOpenTopic,
  onTopicToCard,
  focusTopicId,
  onFocusConsumed,
  currentUser,
}: ChannelPageProps) {
  const { toast } = useToast()
  const chan = useChannel(channel.id)
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const [showMentionHint, setShowMentionHint] = useState(false)
  const bottomRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)

  // New message → scroll to the bottom + push the unread watermark (being in the channel = read).
  useEffect(() => {
    if (chan.lastSeq <= 0) return
    bottomRef.current?.scrollIntoView({ block: 'end' })
    channelsApi.markRead(channel.id, chan.lastSeq).catch(() => {})
  }, [channel.id, chan.lastSeq, chan.messages.length])

  // Board backlink jump: auto-open the target topic once replay completes.
  useEffect(() => {
    if (!focusTopicId || !chan.connected) return
    const topic = chan.topics[focusTopicId]
    if (topic) {
      onOpenTopic(topic)
      onFocusConsumed?.()
    }
  }, [focusTopicId, chan.connected, chan.topics, onOpenTopic, onFocusConsumed])

  const mentioned = useMemo(() => MENTION_RE.test(text), [text])

  const send = useCallback(async () => {
    const content = text.trim()
    if (!content || sending) return
    setSending(true)
    try {
      await channelsApi.send(channel.id, content, MENTION_RE.test(content))
      setText('')
      setShowMentionHint(false)
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
  }, [channel.id, text, sending, toast])

  const insertMention = useCallback(() => {
    setText((t) => {
      // A '@' right before the caret (the mention-hint trigger character) completes to @Agent.
      if (t.endsWith('@')) return `${t}Agent `
      const sep = t && !t.endsWith(' ') ? ' ' : ''
      return `${t}${sep}@Agent `
    })
    setShowMentionHint(false)
    inputRef.current?.focus()
  }, [])

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Main stream */}
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-6">
        <div className="mx-auto flex max-w-3xl flex-col gap-1">
          {!chan.connected && chan.messages.length === 0 ? (
            <div className="space-y-2 pt-4">
              {[0, 1, 2].map((i) => (
                <div key={i} className="h-10 animate-pulse rounded-lg bg-surface-2" />
              ))}
            </div>
          ) : chan.messages.length === 0 ? (
            <p className="pt-16 text-center text-[13px] leading-relaxed text-ink-3">
              No messages yet.
              <br />
              Discuss with members, or start a topic with @Agent.
            </p>
          ) : (
            chan.messages.map((m) => (
              <MessageRow
                key={m.seq}
                message={m}
                topic={m.topic_id ? chan.topics[m.topic_id] : undefined}
                mine={m.author === currentUser}
                onOpenTopic={onOpenTopic}
                onTopicToCard={onTopicToCard}
              />
            ))
          )}
          <div ref={bottomRef} />
        </div>
      </div>

      {/* Composer */}
      <div className="shrink-0 px-4 pb-4 sm:px-6">
        <div className="relative mx-auto max-w-3xl">
          {showMentionHint && (
            <button
              type="button"
              onClick={insertMention}
              className="absolute -top-9 left-2 rounded-lg border border-border bg-surface px-2.5 py-1.5 text-[12.5px] text-ink shadow-md hover:bg-surface-2"
            >
              @Agent — start a topic
            </button>
          )}
          <div
            className={cn(
              'flex items-end gap-2 rounded-xl border bg-surface px-3 py-2',
              mentioned ? 'border-accent' : 'border-border',
            )}
          >
            <button
              type="button"
              title="Start a topic with @Agent"
              onClick={insertMention}
              className={cn(
                'mb-0.5 shrink-0 rounded-md border px-1.5 py-0.5 font-mono text-[11px] transition-colors',
                mentioned
                  ? 'border-accent text-accent'
                  : 'border-border text-ink-3 hover:text-ink',
              )}
            >
              @Agent
            </button>
            <textarea
              ref={inputRef}
              value={text}
              rows={1}
              placeholder={
                mentioned ? 'This will start an agent topic…' : `Message #${channel.name}`
              }
              onChange={(e) => {
                setText(e.target.value)
                const v = e.target.value
                setShowMentionHint(v.endsWith('@') && !MENTION_RE.test(v))
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault()
                  void send()
                }
              }}
              className="max-h-40 min-h-[24px] flex-1 resize-none bg-transparent text-[13.5px] leading-6 text-ink outline-none placeholder:text-ink-3"
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
          <p className="mt-1.5 px-1 text-[11px] text-ink-3">
            Enter to send · Shift+Enter for a new line · messages with @Agent start a topic
          </p>
        </div>
      </div>
    </div>
  )
}

const STATUS_META: Record<string, { label: string; cls: string }> = {
  running: { label: 'Running', cls: 'text-accent border-accent/40' },
  waiting: { label: 'Waiting for input', cls: 'text-warn border-warn/40' },
  idle: { label: 'Completed', cls: 'text-ink-3 border-border' },
}

function MessageRow({
  message,
  topic,
  mine,
  onOpenTopic,
  onTopicToCard,
}: {
  message: ChannelMessage
  topic?: ChannelTopic
  mine: boolean
  onOpenTopic: (topic: ChannelTopic) => void
  onTopicToCard?: (topic: ChannelTopic) => void
}) {
  return (
    <div className="group rounded-lg px-2 py-1.5 hover:bg-surface-2/50">
      <div className="flex items-baseline gap-2">
        <span
          className={cn(
            'text-[12.5px] font-semibold',
            mine ? 'text-accent' : 'text-ink',
          )}
        >
          {message.author}
        </span>
        <span className="font-mono text-[10.5px] text-ink-3">
          {relativeTime(message.created_at)}
        </span>
      </div>
      <div className="mt-0.5 text-[13.5px] leading-relaxed text-ink">
        <Markdown text={message.text} />
      </div>
      {topic && (
        <TopicCard topic={topic} onOpen={onOpenTopic} onToCard={onTopicToCard} />
      )}
    </div>
  )
}

/** Topic card: enhanced rendering of the root message — status + latest reply preview + open / to-card entries. */
function TopicCard({
  topic,
  onOpen,
  onToCard,
}: {
  topic: ChannelTopic
  onOpen: (topic: ChannelTopic) => void
  onToCard?: (topic: ChannelTopic) => void
}) {
  const meta = STATUS_META[topic.status] ?? STATUS_META.running
  return (
    <div className="mt-1.5 rounded-lg border border-border bg-surface px-3 py-2">
      <div className="flex items-center gap-2">
        <span
          className={cn(
            'shrink-0 rounded border px-1.5 py-0.5 font-mono text-[10px]',
            meta.cls,
          )}
        >
          {topic.status === 'running' && (
            <span className="mr-1 inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-accent align-middle" />
          )}
          Topic · {meta.label}
        </span>
        <span className="min-w-0 flex-1 truncate text-[12px] text-ink-3">
          {topic.last_reply_preview || 'The agent is working…'}
        </span>
      </div>
      <div className="mt-1.5 flex items-center gap-2">
        <button
          type="button"
          onClick={() => onOpen(topic)}
          className="rounded-md border border-border bg-bg px-2 py-1 text-[12px] text-ink transition-colors hover:border-border-strong hover:bg-surface-2"
        >
          Open topic
        </button>
        {onToCard && (
          <button
            type="button"
            onClick={() => onToCard(topic)}
            className="flex items-center gap-1 rounded-md px-2 py-1 text-[12px] text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink"
          >
            <IconPlus className="h-3 w-3" />
            To board card
          </button>
        )}
      </div>
    </div>
  )
}
