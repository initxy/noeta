// Default theme plus a thin brand layer (custom.css) and the reader-facing
// diagrams. Each diagram is an inline-SVG component in ./diagrams with a
// `lang` prop, so both locales share one drawing and it follows dark mode.
// Every ./diagrams/Nt*.vue file is registered globally under its file name.
import DefaultTheme from 'vitepress/theme'
import type { Component } from 'vue'
import type { Theme } from 'vitepress'
import './custom.css'

const diagrams = import.meta.glob<{ default: Component }>('./diagrams/Nt*.vue', { eager: true })

export default {
  extends: DefaultTheme,
  enhanceApp({ app }) {
    for (const [path, mod] of Object.entries(diagrams)) {
      const name = path.split('/').pop()!.replace(/\.vue$/, '')
      app.component(name, mod.default)
    }
  },
} satisfies Theme
