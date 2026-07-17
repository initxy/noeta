/**
 * Copy text to the clipboard, compatible with non-secure contexts (HTTP that
 * is not localhost).
 *
 * Prefers the modern Clipboard API (`navigator.clipboard.writeText`) and
 * falls back to `document.execCommand('copy')` when unsupported or denied.
 */
export async function copyText(text: string): Promise<void> {
  // Modern Clipboard API: only available in secure contexts (HTTPS / localhost).
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return
    } catch {
      // Possibly a permission denial or a non-secure context; continue to the fallback.
    }
  }

  // Fallback: hidden textarea + execCommand('copy'), compatible with old browsers and HTTP.
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.top = '-1000px'
  textarea.style.left = '-1000px'
  textarea.style.opacity = '0'
  document.body.appendChild(textarea)
  textarea.select()
  textarea.setSelectionRange(0, text.length)

  let ok = false
  try {
    ok = document.execCommand('copy')
  } finally {
    document.body.removeChild(textarea)
  }

  if (!ok) {
    throw new Error('copy failed')
  }
}
