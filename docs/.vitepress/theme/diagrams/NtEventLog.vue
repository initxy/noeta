<script setup lang="ts">
const props = defineProps<{ lang?: string }>()
const zh = props.lang === 'zh'
const t = zh
  ? {
      engine: ['Engine 把', '发生的事记下来'],
      snap: '快照', snapSub: '只为提速，可以不要',
      log: 'EventLog', logSub: '每个任务一条只追加的流水',
      store: 'ContentStore', storeSub: '大块内容按哈希存',
      fold: 'fold()', foldSub: '按顺序从头重放',
      state: '任务当前状态', stateSub: ['对话、目标和待办、', '上下文、计数'],
      small: '小事件，4 KB 以内', big: '大块内容', ref: '事件里只留 ContentRef',
      start: '从这里开始', after: '之后的事件', hash: '按哈希取', next: '下一步',
      aria: '事件溯源：Engine 把事件追加进日志，fold 从日志重新算出任务状态',
    }
  : {
      engine: ['Engine records', 'what happened'],
      snap: 'Snapshot', snapSub: 'optional shortcut',
      log: 'EventLog', logSub: 'one append-only stream per Task',
      store: 'ContentStore', storeSub: 'large bodies, by hash',
      fold: 'fold()', foldSub: 'replay in order',
      state: 'Task state', stateSub: ['messages · goal & todos', 'context · counters'],
      small: 'small event ≤ 4 KB', big: 'big body', ref: 'the event keeps a ContentRef',
      start: 'start here', after: 'events after it', hash: 'look up by hash', next: 'next step',
      aria: 'Event sourcing: the Engine appends events to the log; fold recomputes Task state from it',
    }
</script>

<template>
  <figure class="ntd">
    <svg viewBox="0 0 800 356" role="img" :aria-label="t.aria">
      <defs>
        <marker id="nte-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" class="ntd-arrowhead" />
        </marker>
      </defs>

      <!-- A: engine -->
      <rect x="16" y="110" width="140" height="70" rx="10" class="ntd-chip ntd-chip-green" />
      <text x="86" y="140" class="ntd-chiptext" text-anchor="middle">{{ t.engine[0] }}</text>
      <text x="86" y="160" class="ntd-chiptext" text-anchor="middle">{{ t.engine[1] }}</text>

      <!-- D: snapshot -->
      <rect x="286" y="16" width="214" height="58" rx="10" class="ntd-chip ntd-chip-violet ntd-dash" />
      <text x="393" y="40" class="ntd-title" text-anchor="middle">{{ t.snap }}</text>
      <text x="393" y="61" class="ntd-sub" text-anchor="middle">{{ t.snapSub }}</text>

      <!-- B: event log -->
      <rect x="286" y="113" width="214" height="64" rx="10" class="ntd-chip ntd-chip-violet" />
      <text x="393" y="139" class="ntd-title" text-anchor="middle">{{ t.log }}</text>
      <text x="393" y="161" class="ntd-sub" text-anchor="middle">{{ t.logSub }}</text>

      <!-- C: content store -->
      <rect x="286" y="218" width="214" height="64" rx="10" class="ntd-chip ntd-chip-violet" />
      <text x="393" y="244" class="ntd-title" text-anchor="middle">{{ t.store }}</text>
      <text x="393" y="266" class="ntd-sub" text-anchor="middle">{{ t.storeSub }}</text>

      <!-- E: fold -->
      <rect x="610" y="113" width="174" height="64" rx="10" class="ntd-box" />
      <text x="697" y="140" class="ntd-code" text-anchor="middle">{{ t.fold }}</text>
      <text x="697" y="161" class="ntd-sub" text-anchor="middle">{{ t.foldSub }}</text>

      <!-- F: task state -->
      <rect x="610" y="226" width="174" height="86" rx="10" class="ntd-box" />
      <text x="697" y="252" class="ntd-title" text-anchor="middle">{{ t.state }}</text>
      <text x="697" y="274" class="ntd-sub" text-anchor="middle">{{ t.stateSub[0] }}</text>
      <text x="697" y="294" class="ntd-sub" text-anchor="middle">{{ t.stateSub[1] }}</text>

      <!-- A → B, A → C -->
      <line x1="156" y1="145" x2="284" y2="145" class="ntd-line" marker-end="url(#nte-arrow)" />
      <text x="220" y="137" class="ntd-mini" text-anchor="middle">{{ t.small }}</text>
      <line x1="156" y1="170" x2="284" y2="246" class="ntd-line ntd-dashed" marker-end="url(#nte-arrow)" />
      <text x="214" y="219" class="ntd-mini ntd-strong" text-anchor="end">{{ t.big }}</text>
      <text x="238" y="236" class="ntd-mini" text-anchor="end">{{ t.ref }}</text>

      <!-- D, B, C → E -->
      <path d="M 500 45 H 697 V 111" class="ntd-line" fill="none" marker-end="url(#nte-arrow)" />
      <text x="600" y="38" class="ntd-mini" text-anchor="middle">{{ t.start }}</text>
      <line x1="500" y1="145" x2="608" y2="145" class="ntd-line" marker-end="url(#nte-arrow)" />
      <text x="554" y="137" class="ntd-mini" text-anchor="middle">{{ t.after }}</text>
      <path d="M 500 250 H 588 V 166 H 608" class="ntd-line" fill="none" marker-end="url(#nte-arrow)" />
      <text x="508" y="268" class="ntd-mini">{{ t.hash }}</text>

      <!-- E → F → A -->
      <line x1="697" y1="177" x2="697" y2="224" class="ntd-line" marker-end="url(#nte-arrow)" />
      <path d="M 697 312 V 338 H 50 V 182" class="ntd-line ntd-dashed" fill="none" marker-end="url(#nte-arrow)" />
      <text x="374" y="331" class="ntd-mini" text-anchor="middle">{{ t.next }}</text>
    </svg>
  </figure>
</template>
