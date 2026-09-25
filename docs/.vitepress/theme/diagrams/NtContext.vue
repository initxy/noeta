<script setup lang="ts">
const props = defineProps<{ lang?: string }>()
const zh = props.lang === 'zh'
const t = zh
  ? {
      aria: '三类输入经 ThreeSegmentComposer 拼成三段，前面走缓存，只有末尾每步新增',
      inputs: [
        ['系统提示词和工具定义', 'agent 身份（AgentSpec）'],
        ['常驻内容', '技能、记忆索引、', '环境信息'],
        ['到目前为止的对话', '从日志重建出来'],
      ],
      same: '同样的状态，同样的字节',
      stable: '几乎不变', semi: '第一次回复前定下',
      old: '之前的对话', fresh: ['这一步新增的消息', '和提醒'],
      cached: '走缓存', cachedSub: '原样重用', fresh2: '每步新增',
      policy: 'Policy 生成',
      cache: ['服务商缓存', '前面重用，', '只有末尾是新的'],
    }
  : {
      aria: 'Three inputs are composed by ThreeSegmentComposer into three segments; the head is cached and only the tail is new each step',
      inputs: [
        ['Agent prompt + tools', 'agent identity (AgentSpec)'],
        ['Standing content', 'skills, memory index,', 'environment facts'],
        ['Conversation so far', 'folded from the log'],
      ],
      same: 'same state → same bytes',
      stable: 'almost never changes', semi: 'fixed before the first reply',
      old: 'conversation so far', fresh: ["this step's new messages", '+ reminders'],
      cached: 'cached', cachedSub: 'reused as-is', fresh2: 'new each step',
      policy: 'Policy builds the',
      cache: ['Provider cache', 'head reused,', 'only the tail is new'],
    }
const iy = [24, 104, 184]
</script>

<template>
  <figure class="ntd">
    <svg viewBox="0 0 760 372" role="img" :aria-label="t.aria">
      <defs>
        <marker id="ntc-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" class="ntd-arrowhead" />
        </marker>
      </defs>

      <!-- inputs -->
      <g v-for="(inp, i) in t.inputs" :key="inp[0]">
        <rect x="16" :y="iy[i]" width="180" height="68" rx="10" :class="['ntd-box', i === 2 ? 'ntd-violet' : '']" />
        <text x="106" :y="iy[i] + (inp.length > 2 ? 22 : 29)" class="ntd-chiptext" text-anchor="middle">{{ inp[0] }}</text>
        <text v-for="(s, j) in inp.slice(1)" :key="s" x="106" :y="iy[i] + (inp.length > 2 ? 41 : 48) + j * 15" class="ntd-mini" text-anchor="middle">{{ s }}</text>
        <line x1="196" :y1="iy[i] + 34" x2="230" :y2="iy[i] + 34" class="ntd-line" marker-end="url(#ntc-arrow)" />
      </g>

      <!-- composer -->
      <rect x="232" y="24" width="168" height="228" rx="10" class="ntd-box ntd-green" />
      <text x="316" y="124" class="ntd-code" text-anchor="middle">ThreeSegmentComposer</text>
      <foreignObject x="244" y="136" width="144" height="48"><div class="ntd-fo">{{ t.same }}</div></foreignObject>
      <line x1="400" y1="138" x2="434" y2="138" class="ntd-line" marker-end="url(#ntc-arrow)" />

      <!-- three segments -->
      <rect x="436" y="24" width="180" height="52" rx="8" class="ntd-box" />
      <text x="526" y="45" class="ntd-code" text-anchor="middle">stable_prefix</text>
      <text x="526" y="64" class="ntd-mini" text-anchor="middle">{{ t.stable }}</text>

      <rect x="436" y="84" width="180" height="52" rx="8" class="ntd-box" />
      <text x="526" y="105" class="ntd-code" text-anchor="middle">semi_stable</text>
      <text x="526" y="124" class="ntd-mini" text-anchor="middle">{{ t.semi }}</text>

      <rect x="436" y="144" width="180" height="108" rx="8" class="ntd-box" />
      <text x="526" y="165" class="ntd-code" text-anchor="middle">dynamic_suffix</text>
      <text x="526" y="184" class="ntd-mini" text-anchor="middle">{{ t.old }}</text>
      <line x1="446" y1="199" x2="606" y2="199" class="ntd-line ntd-dashed" />
      <text v-for="(s, j) in t.fresh" :key="s" x="526" :y="220 + j * 15" class="ntd-mini ntd-strong" text-anchor="middle">{{ s }}</text>

      <!-- brackets -->
      <path d="M 626 26 L 632 26 L 632 196 L 626 196" class="ntd-line" fill="none" />
      <text x="642" y="106" class="ntd-mini ntd-strong">{{ t.cached }}</text>
      <text x="642" y="122" class="ntd-mini">{{ t.cachedSub }}</text>
      <path d="M 626 202 L 632 202 L 632 250 L 626 250" class="ntd-line" fill="none" />
      <text x="642" y="230" class="ntd-mini ntd-strong">{{ t.fresh2 }}</text>

      <!-- policy + provider -->
      <line x1="516" y1="252" x2="516" y2="290" class="ntd-line" marker-end="url(#ntc-arrow)" />
      <rect x="436" y="292" width="160" height="62" rx="10" class="ntd-box ntd-green" />
      <text x="516" y="317" class="ntd-chiptext" text-anchor="middle">{{ t.policy }}</text>
      <text x="516" y="337" class="ntd-code" text-anchor="middle">LLMRequest</text>

      <line x1="596" y1="323" x2="622" y2="323" class="ntd-line" marker-end="url(#ntc-arrow)" />
      <rect x="624" y="287" width="120" height="72" rx="10" class="ntd-box ntd-amber" />
      <text x="684" y="309" class="ntd-chiptext" text-anchor="middle">{{ t.cache[0] }}</text>
      <text v-for="(s, j) in t.cache.slice(1)" :key="s" x="684" :y="328 + j * 15" class="ntd-mini" text-anchor="middle">{{ s }}</text>
    </svg>
  </figure>
</template>
