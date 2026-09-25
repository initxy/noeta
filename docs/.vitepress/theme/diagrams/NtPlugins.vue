<script setup lang="ts">
const props = defineProps<{ lang?: string }>()
const zh = props.lang === 'zh'
// A line starting with ` renders as code.
const t = zh
  ? {
      aria: '插件和内置插件走同一条路：读清单、按扩展点检查、构建 Client 时导入代码，再按三种方式生效',
      top: [
        ['你的插件', '和内置插件', '走同一条路'],
        ['`load_plugins()', '只读清单，', '不执行代码'],
        ['按 16 个扩展点检查', '有冲突直接报错', '`SurfaceRegistry'],
        ['`PluginSet.resolve()', '构建 Client 时', '才导入代码'],
      ],
      bottom: [
        ['按 agent 选', '`Options.plugins', '工具、提示词、策略、', '提醒等'],
        ['整个进程都生效', '`guard、observer', '进程里所有 agent，', '不能跳过'],
        ['宿主程序挑选', '`provider、sandbox_provider', '`mcp_server、skills', '自动接上'],
      ],
      note: '只有身份类扩展点计入 agent 身份',
    }
  : {
      aria: 'Plugins and built-ins take the same path: read manifests, check against surfaces, import code at Client build, then take effect in three ways',
      top: [
        ['Your plugin', 'and every built-in:', 'same path'],
        ['`load_plugins()', 'reads manifests,', 'runs no code'],
        ['16 surfaces', 'checked, collisions fail', '`SurfaceRegistry'],
        ['`PluginSet.resolve()', 'at Client build:', 'imports the code'],
      ],
      bottom: [
        ['Per agent, picked by', '`Options.plugins', 'tools, prompts, policy,', 'reminders…'],
        ['Whole process', '`guard, observer', 'every agent in the process;', 'no opting out'],
        ['Host picks', '`provider, sandbox_provider', '`mcp_server, skills:', 'joined automatically'],
      ],
      note: 'only identity surfaces count toward agent identity',
    }
const code = (s: string) => s.startsWith('`')
const strip = (s: string) => (code(s) ? s.slice(1) : s)
const tx = [16, 205, 394, 583]
const bx = [16, 267, 518]
</script>

<template>
  <figure class="ntd">
    <svg viewBox="0 0 760 300" role="img" :aria-label="t.aria">
      <defs>
        <marker id="ntp-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" class="ntd-arrowhead" />
        </marker>
      </defs>

      <!-- main line -->
      <g v-for="(n, i) in t.top" :key="n[0]">
        <rect :x="tx[i]" y="20" width="161" height="76" rx="10" class="ntd-box" />
        <text :x="tx[i] + 80.5" y="44" :class="code(n[0]) ? 'ntd-code' : 'ntd-chiptext'" text-anchor="middle">{{ strip(n[0]) }}</text>
        <text v-for="(s, j) in n.slice(1)" :key="s" :x="tx[i] + 80.5" :y="64 + j * 16" :class="code(s) ? 'ntd-code' : 'ntd-mini'" text-anchor="middle">{{ strip(s) }}</text>
        <line v-if="i < 3" :x1="tx[i] + 161" y1="58" :x2="tx[i + 1] - 2" y2="58" class="ntd-line" marker-end="url(#ntp-arrow)" />
      </g>

      <!-- fan out -->
      <path d="M 663.5 96 L 663.5 126 L 129 126" class="ntd-line" fill="none" />
      <line v-for="x in [129, 380, 631]" :key="x" :x1="x" y1="126" :x2="x" y2="152" class="ntd-line" marker-end="url(#ntp-arrow)" />

      <g v-for="(n, i) in t.bottom" :key="n[0]">
        <rect :x="bx[i]" y="154" width="226" height="130" rx="10" class="ntd-box" />
        <text :x="bx[i] + 113" y="180" class="ntd-chiptext" text-anchor="middle">{{ n[0] }}</text>
        <text v-for="(s, j) in n.slice(1)" :key="s" :x="bx[i] + 113" :y="202 + j * 17" :class="code(s) ? 'ntd-code' : 'ntd-mini'" text-anchor="middle">{{ strip(s) }}</text>
      </g>
      <line x1="36" y1="248" x2="222" y2="248" class="ntd-line ntd-dashed" />
      <foreignObject x="26" y="252" width="206" height="30"><div class="ntd-fo ntd-em">{{ t.note }}</div></foreignObject>
    </svg>
  </figure>
</template>
