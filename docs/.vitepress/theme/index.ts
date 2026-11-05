// Default theme, a thin brand layer (custom.css) and <Diagram name="…" />, which inlines
// docs/diagrams/<name>.<lang>.svg so the page's language and the theme toggle both apply.
import DefaultTheme from 'vitepress/theme'
import type { Theme } from 'vitepress'
import Diagram from './Diagram.vue'
import './custom.css'
import './diagrams.generated.css'

export default {
  extends: DefaultTheme,
  enhanceApp({ app }) {
    app.component('Diagram', Diagram)
  },
} satisfies Theme
