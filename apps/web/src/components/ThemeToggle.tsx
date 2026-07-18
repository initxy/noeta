import { useTheme } from '../state/theme'
import { IconMoon, IconSun } from './icons'

/** Light / dark two-way switch: clicking flips to the other theme. */
export function ThemeToggle() {
  const { mode, toggle } = useTheme()
  const Icon = mode === 'light' ? IconSun : IconMoon
  return (
    <button
      type="button"
      onClick={toggle}
      title={mode === 'light' ? 'Switch to dark theme' : 'Switch to light theme'}
      className="flex h-8 w-8 items-center justify-center rounded-lg text-ink-2 transition-colors hover:bg-surface-2 hover:text-ink focus-visible:outline-2 focus-visible:outline-accent"
    >
      <Icon />
    </button>
  )
}
