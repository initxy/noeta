<script setup lang="ts">
const props = defineProps<{ lang?: string }>()
const zh = props.lang === 'zh'
const t = zh
  ? {
      a: ['Worker A 拿着租约', '在跑一步'],
      b: 'A 在半路挂了',
      c: '租约过期，任务重新排队',
      log: '事件日志', late: '迟到的写入被拒（InvalidLease）',
      d: ['Worker B 拿到租约，', '从日志重建状态'],
      e: '把做了一半的那次尝试标记作废',
      f: '重跑这一步', g: ['停下来，等人处理'],
      safe: ['可以安全重跑', '不需要审批'], human: '需要人判断',
      cap: '连续 3 次作废 → 停下等人',
      aria: '崩溃恢复：Worker A 挂掉，租约过期，Worker B 从日志重建状态，把那次尝试标记作废，然后重跑或等人处理',
    }
  : {
      a: ['Worker A holds the lease,', 'runs a step'],
      b: 'A dies mid-step',
      c: 'Lease expires → back on the queue',
      log: 'Event log', late: 'late write rejected (InvalidLease)',
      d: ['Worker B leases', '+ folds the log'],
      e: 'Mark the half-done attempt dead',
      f: 'Re-run the step', g: ['Park —', 'wait for a human'],
      safe: ['safe to repeat', 'no approval needed'], human: 'needs a human',
      cap: '3 abandons in a row → park',
      aria: 'Crash recovery: Worker A dies, its lease expires, Worker B folds the log, voids the half-done attempt, then re-runs it or parks for a human',
    }
</script>

<template>
  <figure class="ntd">
    <svg viewBox="0 0 800 330" role="img" :aria-label="t.aria">
      <defs>
        <marker id="ntr-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" class="ntd-arrowhead" />
        </marker>
        <marker id="ntr-arrow-red" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" class="ntd-arrowhead-red" />
        </marker>
      </defs>

      <!-- event log + late write -->
      <rect x="256" y="14" width="170" height="36" rx="8" class="ntd-chip ntd-chip-violet" />
      <text x="341" y="37" class="ntd-chiptext" text-anchor="middle">{{ t.log }}</text>
      <line x1="341" y1="80" x2="341" y2="53" class="ntd-red-line ntd-dashed" marker-end="url(#ntr-arrow-red)" />
      <g class="ntd-red-stroke">
        <line x1="334" y1="60" x2="348" y2="74" /><line x1="348" y1="60" x2="334" y2="74" />
      </g>
      <text x="438" y="37" class="ntd-mini ntd-red">{{ t.late }}</text>

      <!-- row 1: A → B → C -->
      <rect x="16" y="80" width="200" height="60" rx="10" class="ntd-chip ntd-chip-green" />
      <text x="116" y="106" class="ntd-chiptext" text-anchor="middle">{{ t.a[0] }}</text>
      <text x="116" y="125" class="ntd-chiptext" text-anchor="middle">{{ t.a[1] }}</text>
      <line x1="216" y1="110" x2="254" y2="110" class="ntd-line" marker-end="url(#ntr-arrow)" />

      <rect x="256" y="80" width="170" height="60" rx="10" class="ntd-chip ntd-chip-red" />
      <text x="341" y="115" class="ntd-chiptext" text-anchor="middle">{{ t.b }}</text>
      <line x1="426" y1="110" x2="464" y2="110" class="ntd-line" marker-end="url(#ntr-arrow)" />

      <rect x="466" y="80" width="318" height="60" rx="10" class="ntd-box" />
      <text x="625" y="106" class="ntd-chiptext" text-anchor="middle">{{ t.c }}</text>
      <text x="625" y="126" class="ntd-code" text-anchor="middle">requeue_stale</text>

      <!-- C → D -->
      <path d="M 520 140 V 168 H 106 V 198" class="ntd-line" fill="none" marker-end="url(#ntr-arrow)" />

      <!-- row 2: D → E -->
      <rect x="16" y="200" width="180" height="64" rx="10" class="ntd-chip ntd-chip-green" />
      <text x="106" y="227" class="ntd-chiptext" text-anchor="middle">{{ t.d[0] }}</text>
      <text x="106" y="247" class="ntd-chiptext" text-anchor="middle">{{ t.d[1] }}</text>
      <line x1="196" y1="232" x2="234" y2="232" class="ntd-line" marker-end="url(#ntr-arrow)" />

      <rect x="236" y="200" width="240" height="64" rx="10" class="ntd-chip ntd-chip-green" />
      <text x="356" y="227" class="ntd-chiptext" text-anchor="middle">{{ t.e }}</text>
      <text x="356" y="248" class="ntd-code" text-anchor="middle">StepAttemptAbandoned</text>

      <!-- E → F / G -->
      <line x1="476" y1="222" x2="618" y2="197" class="ntd-line" marker-end="url(#ntr-arrow)" />
      <text x="545" y="198" class="ntd-mini ntd-strong" text-anchor="middle">{{ t.safe[0] }}</text>
      <line x1="476" y1="242" x2="618" y2="268" class="ntd-line" marker-end="url(#ntr-arrow)" />
      <text x="547" y="276" class="ntd-mini ntd-strong" text-anchor="middle">{{ t.human }}</text>

      <rect x="620" y="170" width="164" height="54" rx="10" class="ntd-chip ntd-chip-green" />
      <text x="702" y="193" class="ntd-chiptext" text-anchor="middle">{{ t.f }}</text>
      <text x="702" y="212" class="ntd-mini" text-anchor="middle">{{ t.safe[1] }}</text>

      <rect x="620" y="244" width="164" height="52" rx="10" class="ntd-chip ntd-chip-amber" />
      <text v-if="t.g.length === 1" x="702" y="275" class="ntd-chiptext" text-anchor="middle">{{ t.g[0] }}</text>
      <template v-else>
        <text x="702" y="266" class="ntd-chiptext" text-anchor="middle">{{ t.g[0] }}</text>
        <text x="702" y="285" class="ntd-chiptext" text-anchor="middle">{{ t.g[1] }}</text>
      </template>
      <text x="702" y="316" class="ntd-mini" text-anchor="middle">{{ t.cap }}</text>
    </svg>
  </figure>
</template>
