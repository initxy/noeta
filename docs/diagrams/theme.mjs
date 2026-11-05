// The one source of the diagrams' look. `node scripts/diagrams.mjs --write` copies it into
// every diagrams/*.svg (so a file opened on its own follows the system's light/dark setting)
// and into .vitepress/theme/diagrams.generated.css (so the site's own toggle drives it).
// Edit here, never in an .svg's <style> block.

export const light = {
  bg: '#ffffff', panel: '#f6f6f7', border: '#c2c2c4', text1: '#3c3c43', text2: '#67676c',
  text3: '#929295', brand: '#059669', green: '#10b981', violet: '#8b5cf6', amber: '#f59e0b',
  cyan: '#0891b2', red: '#e11d48',
  greenSoft: 'rgba(16,185,129,.14)', violetSoft: 'rgba(139,92,246,.13)',
  amberSoft: 'rgba(245,158,11,.14)', cyanSoft: 'rgba(8,145,178,.12)', redSoft: 'rgba(225,29,72,.10)',
}

export const dark = {
  bg: '#1b1b1f', panel: '#202127', border: '#3c3f44', text1: '#dfdfd6', text2: '#98989f',
  text3: '#6a6a71', brand: '#34d399', green: '#10b981', violet: '#a78bfa', amber: '#fbbf24',
  cyan: '#22d3ee', red: '#fb7185',
  greenSoft: 'rgba(16,185,129,.18)', violetSoft: 'rgba(139,92,246,.20)',
  amberSoft: 'rgba(245,158,11,.18)', cyanSoft: 'rgba(34,211,238,.14)', redSoft: 'rgba(251,113,133,.14)',
}

// Classes a diagram may use. Shapes: ntd-box (plain), ntd-chip + a color (ntd-chip-green /
// -violet / -amber / -cyan / -red), ntd-frame (dashed group outline), ntd-dash (dashed
// outline = optional). Text: ntd-title, ntd-label, ntd-sub, ntd-mini, ntd-chiptext, ntd-code,
// ntd-strong, ntd-em, ntd-red. Lines: ntd-line (+ ntd-dashed), ntd-line-green / -violet /
// -amber / -red; arrowheads ntd-arrowhead(-green/-violet/-amber/-red).
export const classes = `
.ntd-bg{fill:var(--ntd-panel)}
.ntd-box{fill:var(--ntd-bg);stroke:var(--ntd-border);stroke-width:1.2}
.ntd-frame{fill:none;stroke:var(--ntd-green);stroke-width:1.4;stroke-dasharray:6 5}
.ntd-dash{stroke-dasharray:5 4}
.ntd-chip{fill:var(--ntd-bg);stroke:var(--ntd-border);stroke-width:1.2}
.ntd-chip-green{fill:var(--ntd-green-soft);stroke:var(--ntd-green)}
.ntd-chip-violet{fill:var(--ntd-violet-soft);stroke:var(--ntd-violet)}
.ntd-chip-amber{fill:var(--ntd-amber-soft);stroke:var(--ntd-amber)}
.ntd-chip-cyan{fill:var(--ntd-cyan-soft);stroke:var(--ntd-cyan)}
.ntd-chip-red{fill:var(--ntd-red-soft);stroke:var(--ntd-red)}
.ntd-title{fill:var(--ntd-text1);font-size:15px;font-weight:600}
.ntd-label{fill:var(--ntd-brand);font-size:13px;font-weight:600}
.ntd-sub{fill:var(--ntd-text2);font-size:12.5px}
.ntd-mini{fill:var(--ntd-text2);font-size:11.5px}
.ntd-strong{fill:var(--ntd-text1);font-weight:600}
.ntd-em{font-style:italic}
.ntd-chiptext{fill:var(--ntd-text1);font-size:13px;font-weight:500}
.ntd-code{fill:var(--ntd-brand);font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.ntd-red{fill:var(--ntd-red)}
.ntd-line{stroke:var(--ntd-text3);stroke-width:1.4;fill:none}
.ntd-dashed{stroke-dasharray:5 4}
.ntd-line-green{stroke:var(--ntd-green);stroke-width:1.6;fill:none}
.ntd-line-violet{stroke:var(--ntd-violet);stroke-width:1.6;fill:none}
.ntd-line-amber{stroke:var(--ntd-amber);stroke-width:1.8;fill:none}
.ntd-line-red{stroke:var(--ntd-red);stroke-width:1.6;fill:none}
.ntd-arrowhead{fill:var(--ntd-text3)}
.ntd-arrowhead-green{fill:var(--ntd-green)}
.ntd-arrowhead-violet{fill:var(--ntd-violet)}
.ntd-arrowhead-amber{fill:var(--ntd-amber)}
.ntd-arrowhead-red{fill:var(--ntd-red)}
`
