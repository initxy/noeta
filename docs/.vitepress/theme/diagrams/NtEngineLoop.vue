<script setup lang="ts">
const props = defineProps<{ lang?: string }>()
const zh = props.lang === 'zh'
// A subtitle line starting with ` renders as code.
const t = zh
  ? {
      aria: '一次 run_one_step 就是一整轮：工具调用在里面循环，只有要等或完成时才退出',
      frame: '一次 run_one_step = 一整轮',
      a: ['你的代码', '通过 query() 或', 'Client 交任务'],
      b: ['Worker', '拿到租约，', '从日志重建状态'],
      c: ['拼出 View', '模型这次要看的', '全部内容'],
      d: ['Policy 做决定', '去问模型'],
      e: ['guard 检查', '放行 / 拒绝 / 要审批'],
      f: ['执行工具', '结果写入日志'],
      g: ['挂起或结束', '释放租约'],
      h: ['把结果交回', '你的代码'],
      tools: '要调工具', loop: '再来一轮', exit: '要等或已完成',
      note: 'Policy 只做决定；每个结果都由 Engine 记进日志。',
    }
  : {
      aria: 'One run_one_step is one turn: tool calls loop inside it, and it exits only to wait or finish',
      frame: 'One run_one_step = one turn',
      a: ['Your code', 'sends a goal via', 'query() / Client'],
      b: ['Worker', 'takes the lease,', 'folds the log'],
      c: ['Compose', 'the View: exactly', 'what the model sees'],
      d: ['Policy decides', 'asks the model'],
      e: ['Guard', 'allow · deny ·', 'require_approval'],
      f: ['Run tools', 'append results', 'to the log'],
      g: ['Suspend / finish', 'release the lease'],
      h: ['Answer back', 'to your code'],
      tools: 'tool calls', loop: 'loop', exit: 'wait / done',
      note: 'The Policy only decides; the Engine records every effect in the log.',
    }
const W = 128
const H = 68
const nodes = [
  { k: 'a', x: 16, y: 56, green: false },
  { k: 'b', x: 174, y: 56, green: true },
  { k: 'c', x: 332, y: 56, green: true },
  { k: 'd', x: 490, y: 56, green: true },
  { k: 'g', x: 648, y: 56, green: true },
  { k: 'f', x: 332, y: 180, green: true },
  { k: 'e', x: 490, y: 180, green: true },
  { k: 'h', x: 16, y: 270, green: false },
].map((n) => ({ ...n, lines: (t as Record<string, unknown>)[n.k] as string[] }))
</script>

<template>
  <figure class="ntd">
    <svg viewBox="0 0 800 352" role="img" :aria-label="t.aria">
      <defs>
        <marker id="nte-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" class="ntd-arrowhead" />
        </marker>
      </defs>

      <!-- one run_one_step -->
      <rect x="160" y="20" width="626" height="244" rx="14" class="ntd-frame" />
      <text x="176" y="42" class="ntd-label">{{ t.frame }}</text>

      <g v-for="n in nodes" :key="n.k">
        <rect :x="n.x" :y="n.y" :width="W" :height="H" rx="10" :class="['ntd-box', n.green ? 'ntd-green' : '']" />
        <text :x="n.x + W / 2" :y="n.y + (n.lines.length > 2 ? 22 : 29)" class="ntd-chiptext" text-anchor="middle">{{ n.lines[0] }}</text>
        <text v-for="(s, i) in n.lines.slice(1)" :key="s" :x="n.x + W / 2" :y="n.y + (n.lines.length > 2 ? 41 : 48) + i * 15" class="ntd-mini" text-anchor="middle">{{ s }}</text>
      </g>

      <!-- main line -->
      <line x1="144" y1="90" x2="172" y2="90" class="ntd-line" marker-end="url(#nte-arrow)" />
      <line x1="302" y1="90" x2="330" y2="90" class="ntd-line" marker-end="url(#nte-arrow)" />
      <line x1="460" y1="90" x2="488" y2="90" class="ntd-line" marker-end="url(#nte-arrow)" />

      <!-- tool-call loop -->
      <line x1="554" y1="124" x2="554" y2="178" class="ntd-line" marker-end="url(#nte-arrow)" />
      <text x="562" y="156" class="ntd-mini">{{ t.tools }}</text>
      <line x1="490" y1="214" x2="462" y2="214" class="ntd-line" marker-end="url(#nte-arrow)" />
      <line x1="396" y1="180" x2="396" y2="126" class="ntd-line" marker-end="url(#nte-arrow)" />
      <text x="388" y="156" class="ntd-mini ntd-strong" text-anchor="end">{{ t.loop }}</text>

      <!-- exit: wait or finish -->
      <line x1="618" y1="90" x2="646" y2="90" class="ntd-line" marker-end="url(#nte-arrow)" />
      <text x="632" y="50" class="ntd-mini" text-anchor="middle">{{ t.exit }}</text>
      <path d="M 712 124 L 712 304 L 146 304" class="ntd-line" fill="none" marker-end="url(#nte-arrow)" />

      <foreignObject x="176" y="150" width="116" height="100"><div class="ntd-fo">{{ t.note }}</div></foreignObject>
    </svg>
  </figure>
</template>
