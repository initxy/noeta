<script setup lang="ts">
const props = defineProps<{ lang?: string }>()
const zh = props.lang === 'zh'
const t = zh
  ? {
      a: '任务挂起，记下', b: '人、子任务、外部事件', c: '定时器',
      d: ['存下匹配结果，', '重新排队'],
      e: ['Worker 拿到租约，', '唤醒信号随租约带过来'],
      f: ['Engine 写 TaskWoken', '（从这一刻算数）'],
      g: ['释放租约，', '标记唤醒已用掉'],
      match: '匹配', due: '时间到',
      crash: '释放前崩溃，同一个唤醒重发',
      aria: '唤醒：唤醒先存下来，直到 TaskWoken 写入才算用掉；中途崩溃会重发同一个唤醒',
    }
  : {
      a: 'Task suspended with', b: 'human · subtask · external event', c: 'Timer',
      d: ['stores the match,', 're-queues'],
      e: ['Worker leases;', 'the wake rides on the lease'],
      f: ['Engine writes TaskWoken', '(commit point)'],
      g: ['Release:', 'wake marked consumed'],
      match: 'matches', due: 'deadline passed',
      crash: 'crash before release → same wake again',
      aria: 'Waking: the wake is stored until TaskWoken is written; a crash re-delivers the same wake',
    }
</script>

<template>
  <figure class="ntd">
    <svg viewBox="0 0 800 272" role="img" :aria-label="t.aria">
      <defs>
        <marker id="ntk-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" class="ntd-arrowhead" />
        </marker>
        <marker id="ntk-arrow-red" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" class="ntd-arrowhead-red" />
        </marker>
      </defs>

      <!-- left: A, B, C -->
      <rect x="16" y="20" width="220" height="56" rx="10" class="ntd-box ntd-dash" />
      <text x="126" y="43" class="ntd-chiptext" text-anchor="middle">{{ t.a }}</text>
      <text x="126" y="63" class="ntd-code" text-anchor="middle">wake_on</text>

      <rect x="16" y="96" width="220" height="56" rx="10" class="ntd-chip ntd-chip-amber" />
      <text x="126" y="119" class="ntd-chiptext" text-anchor="middle">{{ t.b }}</text>
      <text x="126" y="139" class="ntd-code" text-anchor="middle">Dispatcher.wake</text>

      <rect x="16" y="172" width="220" height="56" rx="10" class="ntd-chip ntd-chip-amber" />
      <text x="126" y="195" class="ntd-chiptext" text-anchor="middle">{{ t.c }}</text>
      <text x="126" y="215" class="ntd-code" text-anchor="middle">fire_due_timers</text>

      <!-- D: dispatcher -->
      <rect x="340" y="72" width="176" height="100" rx="10" class="ntd-chip ntd-chip-violet" />
      <text x="428" y="104" class="ntd-title" text-anchor="middle">Dispatcher</text>
      <text x="428" y="128" class="ntd-sub" text-anchor="middle">{{ t.d[0] }}</text>
      <text x="428" y="148" class="ntd-sub" text-anchor="middle">{{ t.d[1] }}</text>

      <line x1="236" y1="48" x2="338" y2="92" class="ntd-line" marker-end="url(#ntk-arrow)" />
      <line x1="236" y1="124" x2="338" y2="124" class="ntd-line" marker-end="url(#ntk-arrow)" />
      <text x="288" y="116" class="ntd-mini" text-anchor="middle">{{ t.match }}</text>
      <text x="288" y="140" class="ntd-code" text-anchor="middle">wake_on</text>
      <line x1="236" y1="200" x2="338" y2="156" class="ntd-line" marker-end="url(#ntk-arrow)" />
      <text x="296" y="214" class="ntd-mini" text-anchor="middle">{{ t.due }}</text>

      <!-- right: E, F, G -->
      <rect x="580" y="20" width="204" height="60" rx="10" class="ntd-chip ntd-chip-green" />
      <text x="682" y="45" class="ntd-chiptext" text-anchor="middle">{{ t.e[0] }}</text>
      <text x="682" y="65" class="ntd-sub" text-anchor="middle">{{ t.e[1] }}</text>

      <rect x="580" y="106" width="204" height="60" rx="10" class="ntd-chip ntd-chip-green" />
      <text x="682" y="131" class="ntd-chiptext" text-anchor="middle">{{ t.f[0] }}</text>
      <text x="682" y="151" class="ntd-sub" text-anchor="middle">{{ t.f[1] }}</text>

      <rect x="580" y="192" width="204" height="60" rx="10" class="ntd-chip ntd-chip-green" />
      <text x="682" y="217" class="ntd-chiptext" text-anchor="middle">{{ t.g[0] }}</text>
      <text x="682" y="237" class="ntd-sub" text-anchor="middle">{{ t.g[1] }}</text>

      <line x1="516" y1="100" x2="578" y2="56" class="ntd-line" marker-end="url(#ntk-arrow)" />
      <line x1="682" y1="80" x2="682" y2="104" class="ntd-line" marker-end="url(#ntk-arrow)" />
      <line x1="682" y1="166" x2="682" y2="190" class="ntd-line" marker-end="url(#ntk-arrow)" />

      <!-- crash before release → back to the dispatcher -->
      <path d="M 578 206 H 428 V 175" class="ntd-red-line ntd-dashed" fill="none" marker-end="url(#ntk-arrow-red)" />
      <text x="572" y="226" class="ntd-mini ntd-red" text-anchor="end">{{ t.crash }}</text>
    </svg>
  </figure>
</template>
