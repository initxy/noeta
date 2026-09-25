<script setup lang="ts">
const props = defineProps<{ lang?: string }>()
const zh = props.lang === 'zh'
const t = zh
  ? {
      a: 'Worker A', log: '事件日志', b: 'Worker B', step: '第', stepS: ' 步',
      running: '在跑', killed: '被杀掉', lease: '租约到期',
      replay: '重放日志', carry: '从第 4 步接着跑',
    }
  : {
      a: 'Worker A', log: 'Event log', b: 'Worker B', step: 'step ', stepS: '',
      running: 'running', killed: 'killed', lease: 'lease expires',
      replay: 'replay log', carry: 'carries on from step 4',
    }
const aSteps = [0, 1, 2].map((i) => 150 + i * 88)
const bSteps = [0, 1, 2].map((i) => 500 + i * 88)
</script>

<template>
  <figure class="ntd">
    <svg viewBox="0 0 760 240" role="img" aria-label="Worker A dies mid-task; Worker B replays the event log and carries on">
      <defs>
        <marker id="ntc-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" class="ntd-arrowhead" />
        </marker>
      </defs>
      <text x="20" y="55" class="ntd-title">{{ t.a }}</text>
      <text x="20" y="130" class="ntd-title">{{ t.log }}</text>
      <text x="20" y="205" class="ntd-title">{{ t.b }}</text>

      <!-- worker A -->
      <rect x="150" y="34" width="252" height="32" rx="8" class="ntd-chip ntd-chip-green" />
      <text x="276" y="55" class="ntd-chiptext" text-anchor="middle">{{ t.running }}</text>
      <g class="ntd-red-stroke">
        <line x1="420" y1="38" x2="444" y2="62" /><line x1="444" y1="38" x2="420" y2="62" />
      </g>
      <text x="454" y="55" class="ntd-sub ntd-red">{{ t.killed }} · {{ t.lease }}</text>

      <!-- log -->
      <g v-for="(x, i) in aSteps" :key="'a' + i">
        <line :x1="x + 38" y1="66" :x2="x + 38" y2="106" class="ntd-line" marker-end="url(#ntc-arrow)" />
        <rect :x="x" y="108" width="76" height="36" rx="8" class="ntd-chip ntd-chip-violet" />
        <text :x="x + 38" y="131" class="ntd-chiptext" text-anchor="middle">{{ t.step }}{{ i + 1 }}{{ t.stepS }}</text>
      </g>
      <g v-for="(x, i) in bSteps" :key="'b' + i">
        <line :x1="x + 38" y1="184" :x2="x + 38" y2="146" class="ntd-line" marker-end="url(#ntc-arrow)" />
        <rect :x="x" y="108" width="76" height="36" rx="8" class="ntd-chip ntd-chip-violet" />
        <text :x="x + 38" y="131" class="ntd-chiptext" text-anchor="middle">{{ t.step }}{{ i + 4 }}{{ t.stepS }}</text>
      </g>

      <!-- worker B -->
      <rect x="360" y="184" width="118" height="32" rx="8" class="ntd-chip ntd-chip-amber" />
      <text x="419" y="205" class="ntd-chiptext" text-anchor="middle">{{ t.replay }}</text>
      <path d="M 300 146 Q 330 190 358 198" class="ntd-line ntd-dashed" fill="none" marker-end="url(#ntc-arrow)" />
      <rect x="490" y="184" width="256" height="32" rx="8" class="ntd-chip ntd-chip-green" />
      <text x="618" y="205" class="ntd-chiptext" text-anchor="middle">{{ t.carry }}</text>
    </svg>
  </figure>
</template>
