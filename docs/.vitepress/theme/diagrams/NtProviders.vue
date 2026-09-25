<script setup lang="ts">
const props = defineProps<{ lang?: string }>()
const zh = props.lang === 'zh'
// A line starting with ` renders as code.
const t = zh
  ? {
      aria: 'Policy 生成中立的 LLMRequest，经 RuntimeLLMClient 记录后交给你传进来的适配器，再发往厂商接口',
      neutral: '中立的一侧：不联网、不带厂商 SDK',
      sdk: 'noeta-sdk：适配器负责联网',
      row: [
        ['Policy', '生成一个', '`LLMRequest'],
        ['`RuntimeLLMClient', '把这次调用', '记进日志'],
        ['`LLMProvider', '`.complete()', '唯一的中立接口'],
        ['你的适配器', '就是你传进去的那个', '`provider=…'],
        ['厂商接口', 'Anthropic、OpenAI、', '各类网关'],
      ],
      pick: '选一个',
      adapters: '三个可选适配器',
      swap: '换适配器，别的都不用动：Options、工具和已经记下的日志都不变。',
    }
  : {
      aria: 'The Policy builds a neutral LLMRequest; RuntimeLLMClient records it and hands it to the adapter you passed in, which calls the vendor API',
      neutral: 'Vendor-neutral: no network, no vendor SDK',
      sdk: 'noeta-sdk: adapters speak HTTP',
      row: [
        ['Policy', 'builds an', '`LLMRequest'],
        ['`RuntimeLLMClient', 'records the call', 'in the log'],
        ['`LLMProvider', '`.complete()', 'one neutral method'],
        ['Your adapter', 'the one you passed', '`provider=…'],
        ['Vendor API', 'Anthropic, OpenAI,', 'any gateway'],
      ],
      pick: 'pick one',
      adapters: 'Three adapters to choose from',
      swap: 'Swap the adapter and nothing else changes: Options, tools and the recorded log stay the same.',
    }
const code = (s: string) => s.startsWith('`')
const strip = (s: string) => (code(s) ? s.slice(1) : s)
const rx = [16, 174, 332, 490, 648]
const cls = ['ntd-box ntd-green', 'ntd-box ntd-green', 'ntd-box', 'ntd-box', 'ntd-box ntd-amber']
const adapters = ['AnthropicProvider', 'OpenAICompatProvider', 'OpenAIResponsesProvider']
</script>

<template>
  <figure class="ntd">
    <svg viewBox="0 0 800 330" role="img" :aria-label="t.aria">
      <defs>
        <marker id="ntv-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" class="ntd-arrowhead" />
        </marker>
      </defs>

      <!-- boundary -->
      <line x1="478" y1="14" x2="478" y2="316" class="ntd-line ntd-dashed" />
      <text x="468" y="36" class="ntd-label" text-anchor="end">{{ t.neutral }}</text>
      <text x="490" y="36" class="ntd-label">{{ t.sdk }}</text>

      <g v-for="(n, i) in t.row" :key="n[0]">
        <rect :x="rx[i]" y="56" width="134" height="76" rx="10" :class="cls[i]" />
        <text :x="rx[i] + 67" y="80" :class="code(n[0]) ? 'ntd-code' : 'ntd-chiptext'" text-anchor="middle">{{ strip(n[0]) }}</text>
        <text v-for="(s, j) in n.slice(1)" :key="s" :x="rx[i] + 67" :y="100 + j * 16" :class="code(s) ? 'ntd-code' : 'ntd-mini'" text-anchor="middle">{{ strip(s) }}</text>
        <line v-if="i < 4" :x1="rx[i] + 134" y1="94" :x2="rx[i + 1] - 2" y2="94" class="ntd-line" marker-end="url(#ntv-arrow)" />
      </g>

      <foreignObject x="16" y="166" width="440" height="50"><div class="ntd-fo">{{ t.swap }}</div></foreignObject>

      <!-- adapters -->
      <rect x="490" y="176" width="292" height="140" rx="10" class="ntd-box ntd-dash" />
      <text x="636" y="198" class="ntd-chiptext" text-anchor="middle">{{ t.adapters }}</text>
      <text x="636" y="216" class="ntd-code" text-anchor="middle">noeta.sdk.providers</text>
      <g v-for="(a, i) in adapters" :key="a">
        <rect x="536" :y="226 + i * 29" width="200" height="24" rx="7" class="ntd-chip" />
        <text x="636" :y="242 + i * 29" class="ntd-code" text-anchor="middle">{{ a }}</text>
      </g>
      <line x1="520" y1="176" x2="520" y2="134" class="ntd-line ntd-dashed" marker-end="url(#ntv-arrow)" />
      <text x="528" y="160" class="ntd-mini ntd-strong">{{ t.pick }}</text>
    </svg>
  </figure>
</template>
