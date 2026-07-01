// Shared inline SVG icon set. The strings below are trusted constants
// (safe to inject as markup); never route model or network data through innerHTML.

// V3 — the two canonical lucide-react sizes. Every `<Icon size={N}/>` collapses
// to one of these: SM for inline / chip / tool-header / menu glyphs, LG for the
// top header bar, brand mark, composer buttons and other navigation-scale icons.
// Do not introduce other numeric sizes.
const ICON_SM = 14;
const ICON_LG = 16;

const KS_ICONS = {
  alert: '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2l6 11H2z"/><path d="M8 6.5v3M8 11.5h.01"/></svg>',
  bolt: '<svg viewBox="0 0 16 16" width="16" height="16" fill="currentColor"><path d="M9 1L3 9h4l-1 6 6-8H8z"/></svg>',
  brain: '<svg viewBox="0 0 16 16" width="16" height="16" fill="currentColor"><path d="M8 1l1.2 3.2L12.5 5 9.8 7 11 10 8 8.4 5 10l1.2-3L3.5 5l3.3-.8z"/></svg>',
  check: '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8.5l3 3 7-7"/></svg>',
  chevron: '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M6 4l4 4-4 4"/></svg>',
  clock: '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.4"><circle cx="8" cy="8" r="6"/><path d="M8 4.5V8l2.5 1.5" stroke-linecap="round"/></svg>',
  copy: '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M3.5 10.5h-1v-8h8v1"/></svg>',
  file: '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.3"><path d="M4 2h5l3 3v9H4z"/><path d="M9 2v3h3"/></svg>',
  flag: '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 14V2.5h7l-1 2 3 0v5h-9"/></svg>',
  layers: '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"><path d="M8 2l6 3-6 3-6-3z"/><path d="M2 8l6 3 6-3M2 11l6 3 6-3"/></svg>',
  message: '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"><path d="M2.5 3.5h11v7h-7l-3 2.5z"/></svg>',
  moon: '<svg viewBox="0 0 16 16" width="16" height="16" fill="currentColor"><path d="M13 9.5A5.5 5.5 0 016.5 3a5.5 5.5 0 100 11 5.5 5.5 0 006.5-4.5z"/></svg>',
  question: '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6.5"/><path d="M6.2 6a1.8 1.8 0 113 1.4c-.7.5-1.2.8-1.2 1.6"/><path d="M8 11.5h.01"/></svg>',
  robot: '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="3" y="5" width="10" height="8" rx="2"/><path d="M8 3v2M6 9h.01M10 9h.01" stroke-linecap="round"/></svg>',
  search: '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5L14 14" stroke-linecap="round"/></svg>',
  sun: '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.4"><circle cx="8" cy="8" r="3"/><path d="M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.4 1.4M11.6 11.6L13 13M13 3l-1.4 1.4M4.4 11.6L3 13" stroke-linecap="round"/></svg>',
  tool: '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M10.5 2.5a3 3 0 00-4 4l-4 4 2 2 4-4a3 3 0 004-4l-2 2-1.5-1.5z"/></svg>',
  user: '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.3"><circle cx="8" cy="5.5" r="2.5"/><path d="M3.5 13a4.5 4.5 0 019 0"/></svg>',
  x: '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>',
};

export { KS_ICONS, ICON_SM, ICON_LG };
