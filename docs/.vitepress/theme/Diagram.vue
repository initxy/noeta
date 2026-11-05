<script setup lang="ts">
// <Diagram name="event-log" /> inlines docs/diagrams/<name>.<lang>.svg into the page. The
// file's own <style data-ntd> block is dropped: on the site the shared rules come from
// diagrams.generated.css, keyed on the theme toggle instead of the system setting.
// Clicking a diagram shows it full-screen; a click or Escape closes it.
import { computed, ref, watch, onBeforeUnmount } from 'vue'
import { useData } from 'vitepress'

const props = defineProps<{ name: string; caption?: string }>()
const files = import.meta.glob<string>('../../diagrams/*.svg', {
  query: '?raw',
  import: 'default',
  eager: true,
})
const { lang } = useData()
const html = computed(() => {
  const l = lang.value.startsWith('zh') ? 'zh' : 'en'
  const raw = files[`../../diagrams/${props.name}.${l}.svg`]
  if (!raw) return `<p style="color:#e11d48">missing diagram: ${props.name}.${l}.svg</p>`
  return raw.replace(/<\?xml[^>]*>/, '').replace(/<style data-ntd>[\s\S]*?<\/style>/, '')
})
const zoomed = ref(false)
const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') zoomed.value = false }
watch(zoomed, (on) => {
  if (on) window.addEventListener('keydown', onKey)
  else window.removeEventListener('keydown', onKey)
})
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
const hint = computed(() => (lang.value.startsWith('zh') ? '点击放大' : 'Click to enlarge'))
</script>

<template>
  <figure class="ntd">
    <div class="ntd-svg" :title="hint" @click="zoomed = true" v-html="html" />
    <figcaption v-if="caption">{{ caption }}</figcaption>
    <Teleport to="body">
      <div v-if="zoomed" class="ntd ntd-zoom" @click="zoomed = false">
        <div class="ntd-svg" v-html="html" />
      </div>
    </Teleport>
  </figure>
</template>
