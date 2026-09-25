<script setup lang="ts">
const props = defineProps<{ lang?: string }>()
const zh = props.lang === 'zh'
const t = zh
  ? {
      pendingSub: '等 worker 来接', runningSub: '有 Worker 在跑',
      suspendedSub: ['在等 wake_on', '里写的条件'],
      terminalSub: ['已结束：', '完成、失败或取消'],
      pick: '被 Worker 接走', wait: ['要等，或者', '这一轮说完了'],
      woken: '被唤醒', wokenSub: '等的东西到了',
      finish: '完成或失败', cancel: '取消',
      waitsFor: '等什么：', what: ['下一条消息', '审批', '回答', '定时', '子任务', '外部事件'],
      aria: '任务的四种状态：pending、running、suspended、terminal',
    }
  : {
      pendingSub: 'waiting for a worker', runningSub: 'a Worker holds the lease',
      suspendedSub: ['waiting on', 'what wake_on names'],
      terminalSub: ['completed /', 'failed / cancelled'],
      pick: 'a Worker picks it up', wait: ['has to wait, or', 'the turn is done'],
      woken: 'woken', wokenSub: 'the awaited thing arrived',
      finish: 'finish / fail', cancel: 'cancel',
      waitsFor: 'What it waits for:', what: ['next message', 'approval', 'answer', 'timer', 'subtask', 'external event'],
      aria: 'The four Task statuses: pending, running, suspended, terminal',
    }
const events = ['TaskCompleted', 'TaskFailed', 'TaskCancelled']
</script>

<template>
  <figure class="ntd">
    <svg viewBox="0 0 800 372" role="img" :aria-label="t.aria">
      <defs>
        <marker id="nts-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" class="ntd-arrowhead" />
        </marker>
      </defs>

      <!-- A: pending -->
      <rect x="16" y="60" width="150" height="64" rx="10" class="ntd-box" />
      <text x="91" y="87" class="ntd-title" text-anchor="middle">pending</text>
      <text x="91" y="109" class="ntd-sub" text-anchor="middle">{{ t.pendingSub }}</text>

      <!-- B: running -->
      <rect x="300" y="60" width="180" height="64" rx="10" class="ntd-chip ntd-chip-green" />
      <text x="390" y="87" class="ntd-title" text-anchor="middle">running</text>
      <text x="390" y="109" class="ntd-sub" text-anchor="middle">{{ t.runningSub }}</text>

      <!-- C: suspended -->
      <rect x="300" y="210" width="180" height="72" rx="10" class="ntd-box ntd-dash" />
      <text x="390" y="236" class="ntd-title" text-anchor="middle">suspended</text>
      <text x="390" y="256" class="ntd-sub" text-anchor="middle">{{ t.suspendedSub[0] }}</text>
      <text x="390" y="273" class="ntd-sub" text-anchor="middle">{{ t.suspendedSub[1] }}</text>

      <!-- D: terminal -->
      <rect x="600" y="60" width="184" height="222" rx="10" class="ntd-box" />
      <text x="692" y="90" class="ntd-title" text-anchor="middle">terminal</text>
      <text x="692" y="112" class="ntd-sub" text-anchor="middle">{{ t.terminalSub[0] }}</text>
      <text x="692" y="130" class="ntd-sub" text-anchor="middle">{{ t.terminalSub[1] }}</text>
      <g v-for="(e, i) in events" :key="e">
        <rect x="620" :y="150 + i * 42" width="144" height="30" rx="8" class="ntd-chip" />
        <text x="692" :y="170 + i * 42" class="ntd-code" text-anchor="middle">{{ e }}</text>
      </g>

      <!-- A → B -->
      <line x1="166" y1="92" x2="298" y2="92" class="ntd-line" marker-end="url(#nts-arrow)" />
      <text x="232" y="84" class="ntd-mini" text-anchor="middle">{{ t.pick }}</text>

      <!-- B → C -->
      <line x1="390" y1="124" x2="390" y2="208" class="ntd-line" marker-end="url(#nts-arrow)" />
      <text x="380" y="162" class="ntd-mini" text-anchor="end">{{ t.wait[0] }}</text>
      <text x="380" y="177" class="ntd-mini" text-anchor="end">{{ t.wait[1] }}</text>

      <!-- C → A -->
      <path d="M 300 246 H 91 V 126" class="ntd-line" fill="none" marker-end="url(#nts-arrow)" />
      <text x="196" y="239" class="ntd-mini ntd-strong" text-anchor="middle">{{ t.woken }}</text>
      <text x="196" y="262" class="ntd-mini" text-anchor="middle">{{ t.wokenSub }}</text>

      <!-- B → D: finish / fail -->
      <line x1="480" y1="84" x2="598" y2="84" class="ntd-line" marker-end="url(#nts-arrow)" />
      <text x="539" y="76" class="ntd-mini" text-anchor="middle">{{ t.finish }}</text>

      <!-- cancel from A, B, C -->
      <path d="M 91 60 V 30 H 692 V 58" class="ntd-line ntd-dashed" fill="none" marker-end="url(#nts-arrow)" />
      <text x="232" y="24" class="ntd-mini" text-anchor="middle">{{ t.cancel }}</text>
      <line x1="480" y1="108" x2="598" y2="108" class="ntd-line ntd-dashed" marker-end="url(#nts-arrow)" />
      <text x="539" y="122" class="ntd-mini" text-anchor="middle">{{ t.cancel }}</text>
      <line x1="480" y1="246" x2="598" y2="246" class="ntd-line ntd-dashed" marker-end="url(#nts-arrow)" />
      <text x="539" y="238" class="ntd-mini" text-anchor="middle">{{ t.cancel }}</text>

      <!-- E: what suspended waits for -->
      <line x1="390" y1="282" x2="390" y2="314" class="ntd-line ntd-dashed" />
      <rect x="16" y="314" width="768" height="44" rx="10" class="ntd-box ntd-dash" />
      <text x="140" y="341" class="ntd-mini ntd-strong" text-anchor="end">{{ t.waitsFor }}</text>
      <g v-for="(w, i) in t.what" :key="w">
        <rect :x="150 + i * 104" y="322" width="98" height="28" rx="14" class="ntd-chip ntd-chip-amber" />
        <text :x="199 + i * 104" y="340" class="ntd-mini ntd-strong" text-anchor="middle">{{ w }}</text>
      </g>
    </svg>
  </figure>
</template>
