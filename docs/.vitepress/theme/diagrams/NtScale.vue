<script setup lang="ts">
const props = defineProps<{ lang?: string }>()
const zh = props.lang === 'zh'
const t = zh
  ? {
      agent: '同一个 agent：Options(...)，一行不改',
      p: [
        { title: '一个脚本', code: 'query(options, goal=...)', note: '内存或 SQLite' },
        { title: '一个服务', code: 'client.start_workers(4)', note: 'SQLite' },
        { title: '多台机器', code: 'storage_path="postgresql://…"', note: 'Postgres' },
      ],
      host: '机器', proc: '1 个进程',
    }
  : {
      agent: 'The same agent — Options(...) — unchanged',
      p: [
        { title: 'A script', code: 'query(options, goal=...)', note: 'in memory or SQLite' },
        { title: 'A service', code: 'client.start_workers(4)', note: 'SQLite' },
        { title: 'Several hosts', code: 'storage_path="postgresql://…"', note: 'Postgres' },
      ],
      host: 'host', proc: '1 process',
    }
const px = [16, 266, 516]
</script>

<template>
  <figure class="ntd">
    <svg viewBox="0 0 760 300" role="img" :aria-label="t.agent">
      <defs>
        <marker id="nts-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" class="ntd-arrowhead" />
        </marker>
      </defs>
      <rect x="16" y="12" width="728" height="42" rx="10" class="ntd-chip ntd-chip-green" />
      <text x="380" y="39" class="ntd-chiptext" text-anchor="middle">{{ t.agent }}</text>

      <g v-for="(p, i) in t.p" :key="p.title">
        <line :x1="px[i] + 114" y1="54" :x2="px[i] + 114" y2="78" class="ntd-line" marker-end="url(#nts-arrow)" />
        <rect :x="px[i]" y="80" width="228" height="208" rx="12" class="ntd-box" />
        <text :x="px[i] + 16" y="106" class="ntd-title">{{ p.title }}</text>
        <text :x="px[i] + 16" y="128" class="ntd-code" :textLength="p.code.length > 26 ? 196 : undefined" lengthAdjust="spacingAndGlyphs">{{ p.code }}</text>
        <!-- storage -->
        <rect :x="px[i] + 44" y="238" width="140" height="34" rx="8" class="ntd-chip ntd-chip-violet" />
        <text :x="px[i] + 114" y="260" class="ntd-chiptext" text-anchor="middle">{{ p.note }}</text>
      </g>

      <!-- script: one process -->
      <rect x="74" y="152" width="112" height="44" rx="8" class="ntd-chip" />
      <text x="130" y="179" class="ntd-mini ntd-strong" text-anchor="middle">{{ t.proc }}</text>
      <line x1="130" y1="196" x2="130" y2="236" class="ntd-line" marker-end="url(#nts-arrow)" />

      <!-- service: 4 workers -->
      <g v-for="i in 4" :key="'w' + i">
        <rect :x="290 + (i - 1) * 46" y="156" width="38" height="36" rx="7" class="ntd-chip ntd-chip-green" />
        <text :x="309 + (i - 1) * 46" y="179" class="ntd-mini ntd-strong" text-anchor="middle">W{{ i }}</text>
      </g>
      <line x1="380" y1="192" x2="380" y2="236" class="ntd-line" marker-end="url(#nts-arrow)" />

      <!-- hosts -->
      <g v-for="i in 3" :key="'h' + i">
        <rect :x="530 + (i - 1) * 68" y="146" width="62" height="56" rx="8" class="ntd-chip" />
        <text :x="561 + (i - 1) * 68" y="164" class="ntd-mini" text-anchor="middle">{{ t.host }} {{ i }}</text>
        <rect :x="537 + (i - 1) * 68" y="172" width="22" height="22" rx="5" class="ntd-chip ntd-chip-green" />
        <rect :x="563 + (i - 1) * 68" y="172" width="22" height="22" rx="5" class="ntd-chip ntd-chip-green" />
        <line :x1="561 + (i - 1) * 68" y1="202" :x2="630" y2="236" class="ntd-line" marker-end="url(#nts-arrow)" />
      </g>
    </svg>
  </figure>
</template>
