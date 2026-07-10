// Shared inline SVG icon set. The strings below are trusted constants
// (safe to inject as markup); never route model or network data through innerHTML.

// V3 — the two canonical lucide-react sizes. Every `<Icon size={N}/>` collapses
// to one of these: SM for inline / chip / tool-header / menu glyphs, LG for the
// top header bar, brand mark, composer buttons and other navigation-scale icons.
// Do not introduce other numeric sizes.
const ICON_SM = 14;
const ICON_LG = 16;

export { ICON_SM, ICON_LG };
