<script setup lang="ts">
const props = defineProps<{ lang?: string }>()
const zh = props.lang === 'zh'
const t = zh
  ? {
      running: '在跑', waiting: '等待中', waitingSub: '不占线程 · 不占内存 · 不花钱',
      waitsFor: '可以等', what: ['人审批', '定时器', '子任务', '外部事件'],
      wake: '恰好唤醒一次', done: '完成', dur: '几秒，或者几个月',
    }
  : {
      running: 'Running', waiting: 'Waiting', waitingSub: 'no thread · no memory · no cost',
      waitsFor: 'waits for', what: ['Approval', 'Timer', 'Subtask', 'External event'],
      wake: 'woken exactly once', done: 'Done', dur: 'seconds — or months',
    }
</script>

<template>
  <figure class="ntd">
    <svg viewBox="0 0 760 178" role="img" aria-label="A task runs, waits at no cost, is woken exactly once, and finishes">
      <defs>
        <marker id="ntw-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" class="ntd-arrowhead" />
        </marker>
      </defs>
      <!-- what it waits for -->
      <text x="182" y="31" class="ntd-mini" text-anchor="end">{{ t.waitsFor }}</text>
      <g v-for="(w, i) in t.what" :key="w">
        <rect :x="190 + i * 104" y="14" width="98" height="26" rx="13" class="ntd-chip ntd-chip-amber" />
        <text :x="239 + i * 104" y="31" class="ntd-mini ntd-strong" text-anchor="middle">{{ w }}</text>
      </g>

      <rect x="16" y="84" width="150" height="44" rx="10" class="ntd-chip ntd-chip-green" />
      <text x="91" y="111" class="ntd-chiptext" text-anchor="middle">{{ t.running }}</text>
      <line x1="166" y1="106" x2="186" y2="106" class="ntd-line" marker-end="url(#ntw-arrow)" />

      <rect x="188" y="84" width="340" height="44" rx="10" class="ntd-box ntd-dash" />
      <text x="358" y="103" class="ntd-chiptext" text-anchor="middle">{{ t.waiting }}</text>
      <text x="358" y="120" class="ntd-mini" text-anchor="middle">{{ t.waitingSub }}</text>
      <line x1="188" y1="146" x2="528" y2="146" class="ntd-line" marker-start="url(#ntw-arrow)" marker-end="url(#ntw-arrow)" />
      <text x="358" y="166" class="ntd-sub" text-anchor="middle">{{ t.dur }}</text>

      <line x1="528" y1="106" x2="560" y2="106" class="ntd-line ntd-amber-stroke" marker-end="url(#ntw-arrow)" />
      <text x="617" y="74" class="ntd-mini ntd-strong" text-anchor="middle">{{ t.wake }}</text>

      <rect x="562" y="84" width="110" height="44" rx="10" class="ntd-chip ntd-chip-green" />
      <text x="617" y="111" class="ntd-chiptext" text-anchor="middle">{{ t.running }}</text>
      <line x1="672" y1="106" x2="684" y2="106" class="ntd-line" marker-end="url(#ntw-arrow)" />
      <rect x="686" y="84" width="60" height="44" rx="10" class="ntd-box" />
      <text x="716" y="111" class="ntd-chiptext" text-anchor="middle">{{ t.done }}</text>
    </svg>
  </figure>
</template>
