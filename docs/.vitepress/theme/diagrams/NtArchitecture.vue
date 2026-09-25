<script setup lang="ts">
const props = defineProps<{ lang?: string }>()
const zh = props.lang === 'zh'
const t = zh
  ? {
      code: '你的代码', codeSub: 'query() · Client',
      workers: 'worker 池', workersSub: '可选，任何 worker 都能接手任何任务',
      frame: 'Noeta，跑在你自己的进程里',
      loop: '主循环：每一步都这样走', compose: '组装上下文', decide: '模型决定', dispatch: '执行工具',
      repeat: '重复，直到完成或需要等待',
      plugins: '插件：所有能力都走同一套公开接口',
      p: ['文件与 shell', '网页', 'MCP', '记忆', '沙箱', '模型适配器'],
      yours: '你的插件也这样接进来',
      log: '事件日志', logSub: ['SQLite 或 Postgres', '每一步都追加', '状态靠重放得出'],
      append: '追加', replay: '重放',
      model: '模型 API', modelSub: ['Anthropic', '兼容 OpenAI 的网关'],
    }
  : {
      code: 'Your code', codeSub: 'query() · Client',
      workers: 'Worker pool', workersSub: 'optional; any worker runs any task',
      frame: 'Noeta — inside your own process',
      loop: 'Engine loop — every step', compose: 'Compose', decide: 'Decide', dispatch: 'Run tools',
      repeat: 'repeat until done or waiting',
      plugins: 'Plugins — every capability, one public API',
      p: ['Files & shell', 'Web', 'MCP', 'Memory', 'Sandbox', 'Model adapters'],
      yours: 'Your own plugins plug in the same way',
      log: 'Event log', logSub: ['SQLite or Postgres', 'every step appended', 'state = replay'],
      append: 'append', replay: 'replay',
      model: 'Model API', modelSub: ['Anthropic', 'OpenAI-compatible'],
    }
const chips = [0, 1, 2, 3, 4, 5].map((i) => ({ x: 214 + (i % 3) * 110, y: 244 + Math.floor(i / 3) * 44, label: t.p[i], model: i === 5 }))
</script>

<template>
  <figure class="ntd">
    <svg viewBox="0 0 800 390" role="img" :aria-label="t.frame">
      <defs>
        <marker id="nta-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" class="ntd-arrowhead" />
        </marker>
      </defs>

      <!-- your code + workers -->
      <rect x="16" y="120" width="138" height="72" rx="10" class="ntd-box" />
      <text x="85" y="152" class="ntd-title" text-anchor="middle">{{ t.code }}</text>
      <text x="85" y="174" class="ntd-sub" text-anchor="middle">{{ t.codeSub }}</text>

      <rect x="16" y="226" width="138" height="72" rx="10" class="ntd-box ntd-dash" />
      <text x="85" y="256" class="ntd-title" text-anchor="middle">{{ t.workers }}</text>
      <foreignObject x="22" y="264" width="126" height="32"><div class="ntd-fo">{{ t.workersSub }}</div></foreignObject>

      <line x1="154" y1="156" x2="190" y2="156" class="ntd-line" marker-end="url(#nta-arrow)" />
      <line x1="154" y1="262" x2="190" y2="262" class="ntd-line ntd-dashed" marker-end="url(#nta-arrow)" />

      <!-- noeta frame -->
      <rect x="192" y="14" width="370" height="362" rx="14" class="ntd-frame" />
      <text x="210" y="40" class="ntd-label">{{ t.frame }}</text>

      <!-- engine loop -->
      <rect x="206" y="54" width="342" height="128" rx="10" class="ntd-box ntd-green" />
      <text x="222" y="80" class="ntd-title">{{ t.loop }}</text>
      <g v-for="(c, i) in [t.compose, t.decide, t.dispatch]" :key="c">
        <rect :x="222 + i * 110" y="96" width="92" height="36" rx="8" class="ntd-chip ntd-chip-green" />
        <text :x="268 + i * 110" y="119" class="ntd-chiptext" text-anchor="middle">{{ c }}</text>
      </g>
      <line x1="314" y1="114" x2="330" y2="114" class="ntd-line" marker-end="url(#nta-arrow)" />
      <line x1="424" y1="114" x2="440" y2="114" class="ntd-line" marker-end="url(#nta-arrow)" />
      <path d="M 488 132 L 488 152 L 268 152 L 268 134" class="ntd-line" fill="none" marker-end="url(#nta-arrow)" />
      <text x="378" y="170" class="ntd-sub" text-anchor="middle">{{ t.repeat }}</text>

      <!-- plugins -->
      <rect x="206" y="198" width="342" height="164" rx="10" class="ntd-box" />
      <text x="222" y="224" class="ntd-title">{{ t.plugins }}</text>
      <g v-for="c in chips" :key="c.label">
        <rect :x="c.x" :y="c.y" width="100" height="34" rx="8" :class="['ntd-chip', c.model ? 'ntd-chip-amber' : '']" />
        <text :x="c.x + 50" :y="c.y + 22" class="ntd-chiptext" text-anchor="middle">{{ c.label }}</text>
      </g>
      <text x="222" y="348" class="ntd-sub ntd-em">{{ t.yours }}</text>

      <!-- event log -->
      <rect x="628" y="54" width="156" height="128" rx="10" class="ntd-box ntd-violet" />
      <text x="706" y="82" class="ntd-title" text-anchor="middle">{{ t.log }}</text>
      <text v-for="(s, i) in t.logSub" :key="s" x="706" :y="108 + i * 22" class="ntd-sub" text-anchor="middle">{{ s }}</text>
      <line x1="548" y1="104" x2="626" y2="104" class="ntd-line" marker-end="url(#nta-arrow)" />
      <line x1="628" y1="138" x2="550" y2="138" class="ntd-line" marker-end="url(#nta-arrow)" />
      <text x="596" y="97" class="ntd-mini" text-anchor="middle">{{ t.append }}</text>
      <text x="596" y="154" class="ntd-mini" text-anchor="middle">{{ t.replay }}</text>

      <!-- model api -->
      <rect x="628" y="256" width="156" height="84" rx="10" class="ntd-box ntd-amber" />
      <text x="706" y="284" class="ntd-title" text-anchor="middle">{{ t.model }}</text>
      <text v-for="(s, i) in t.modelSub" :key="s" x="706" :y="306 + i * 20" class="ntd-sub" text-anchor="middle">{{ s }}</text>
      <line x1="534" y1="305" x2="626" y2="298" class="ntd-line" marker-end="url(#nta-arrow)" />
    </svg>
  </figure>
</template>
