/** Hand-drawn inline icons: unified stroke style on a 16px grid. */

interface IconProps {
  className?: string
}

function base(props: IconProps) {
  return {
    width: 16,
    height: 16,
    viewBox: '0 0 16 16',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.5,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    className: props.className,
    'aria-hidden': true,
  }
}

export const IconPlus = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M8 3v10M3 8h10" />
  </svg>
)

export const IconTrash = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M2.5 4h11M6.5 4V2.5h3V4M4 4l.7 9a1 1 0 0 0 1 .9h4.6a1 1 0 0 0 1-.9L12 4" />
  </svg>
)

export const IconEdit = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M11.5 2.5a1.4 1.4 0 0 1 2 2L6 12l-3 1 1-3 7.5-7.5Z" />
  </svg>
)

export const IconSend = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M14 2 7.5 8.5M14 2 9.8 14l-2.3-5.5L2 6.2 14 2Z" />
  </svg>
)

export const IconStop = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="4" y="4" width="8" height="8" rx="1.5" fill="currentColor" stroke="none" />
  </svg>
)

export const IconSidebar = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="2" y="3" width="12" height="10" rx="1.5" />
    <path d="M6 3v10" />
  </svg>
)

export const IconPanel = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="2" y="3" width="12" height="10" rx="1.5" />
    <path d="M10 3v10" />
  </svg>
)

export const IconChevron = (p: IconProps & { open?: boolean }) => (
  <svg
    {...base(p)}
    style={{
      transform: p.open ? 'rotate(90deg)' : undefined,
      transition: 'transform 0.15s ease',
    }}
  >
    <path d="M6 4l4 4-4 4" />
  </svg>
)

export const IconRefresh = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9M13.5 2.5V6H10" />
  </svg>
)

export const IconFile = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M9 2H4.5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h7a1 1 0 0 0 1-1V5.5L9 2Z" />
    <path d="M9 2v3.5h3.5" />
  </svg>
)

export const IconFolder = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M2 5a1 1 0 0 1 1-1h3l1.5 1.5H13a1 1 0 0 1 1 1V12a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V5Z" />
  </svg>
)

export const IconFolderOpen = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M2 5a1 1 0 0 1 1-1h3l1.5 1.5H13a1 1 0 0 1 1 1V7H4l-1.5 5L2 6.5V5Z" />
    <path d="M3.5 7H14l-1.5 5a1 1 0 0 1-1 .7H4l-.5-5.7Z" />
  </svg>
)

export const IconSun = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="8" cy="8" r="3" />
    <path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M12.6 3.4l-1.4 1.4M4.8 11.2l-1.4 1.4" />
  </svg>
)

export const IconMoon = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M13.5 9.5A6 6 0 0 1 6.5 2.5a6 6 0 1 0 7 7Z" />
  </svg>
)

export const IconClose = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M4 4l8 8M12 4l-8 8" />
  </svg>
)

export const IconCheck = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M3 8.5 6.5 12 13 4" />
  </svg>
)

export const IconSearch = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="7" cy="7" r="4.5" />
    <path d="M10.5 10.5 14 14" />
  </svg>
)

export const IconChat = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M2.5 3.5h11v7h-6L4.5 13v-2.5h-2v-7Z" />
  </svg>
)

export const IconTrace = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M3 3v10h10" />
    <path d="M5 9.5 8 6l2.5 2.5L13.5 4" />
  </svg>
)

export const IconLogout = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M6.5 2.5H3.5a1 1 0 0 0-1 1v9a1 1 0 0 0 1 1h3M10.5 11l3-3-3-3M13.5 8H6" />
  </svg>
)

export const IconExternal = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M6.5 3.5H3.5a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1V9.5M9.5 2.5h4v4M13.5 2.5 7.5 8.5" />
  </svg>
)

export const IconCopy = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="5.5" y="5.5" width="8" height="8" rx="1.5" />
    <path d="M10.5 3.5a1 1 0 0 0-1-1h-5a2 2 0 0 0-2 2v5a1 1 0 0 0 1 1" />
  </svg>
)

export const IconDoc = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="2.5" y="2" width="11" height="12" rx="1.5" />
    <path d="M5.5 5.5h5M5.5 8h5M5.5 10.5h3" />
  </svg>
)

export const IconSkill = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M6 2.5H4a1.5 1.5 0 0 0-1.5 1.5v2a1.3 1.3 0 1 1 0 2.6v2A1.4 1.4 0 0 0 4 13.5h2a1.3 1.3 0 0 1 2.6 0h2A1.4 1.4 0 0 0 12 12v-2a1.3 1.3 0 0 0 0-2.6V6a1.5 1.5 0 0 0-1.4-1.5h-2A1.3 1.3 0 0 0 6 2.5Z" />
  </svg>
)

export const IconUpload = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M8 10.5V3M5 5.5 8 2.5l3 3M3 11v1.5a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1V11" />
  </svg>
)

export const IconDownload = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M8 2v8M5 7l3 3 3-3M3 11v1.5a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1V11" />
  </svg>
)

export const IconBook = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M3 3h4.5a2 2 0 0 1 2 2v9a1 1 0 0 0-1-1H3V3Z" />
    <path d="M13 3H8.5a2 2 0 0 0-2 2v9a1 1 0 0 1 1-1H13V3Z" />
  </svg>
)

export const IconThumbUp = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M4.5 7v6.5M4.5 7H2.75a.75.75 0 0 0-.75.75v5c0 .41.34.75.75.75H4.5m0-6.5 2.6-4.6a1.3 1.3 0 0 1 2.43.64V5.5h3.12c.87 0 1.5.82 1.3 1.66l-1.16 5.3a1.34 1.34 0 0 1-1.3 1.04H4.5" />
  </svg>
)

export const IconThumbDown = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M11.5 9V2.5M11.5 9h1.75c.41 0 .75-.34.75-.75v-5a.75.75 0 0 0-.75-.75H11.5m0 6.5-2.6 4.6a1.3 1.3 0 0 1-2.43-.64V10.5H3.35c-.87 0-1.5-.82-1.3-1.66l1.16-5.3A1.34 1.34 0 0 1 4.5 2.5h7" />
  </svg>
)

export const IconMemory = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="4.5" y="4.5" width="7" height="7" rx="1.2" />
    <path d="M6.5 4.5v-2M9.5 4.5v-2M6.5 13.5v-2M9.5 13.5v-2M4.5 6.5h-2M4.5 9.5h-2M13.5 6.5h-2M13.5 9.5h-2" />
  </svg>
)

export const IconGit = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="5" cy="4" r="1.5" />
    <circle cx="5" cy="12" r="1.5" />
    <circle cx="11.5" cy="8" r="1.5" />
    <path d="M5 5.5v5M6.3 4.7l3.7 2.2M6.3 11.3l3.7-2.2" />
  </svg>
)

export const IconSync = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M13 5.5A5 5 0 0 0 4.5 4M3 5.5V3h2.5" />
    <path d="M3 10.5A5 5 0 0 0 11.5 12M13 10.5V13h-2.5" />
  </svg>
)

export const IconSettings = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="8" cy="8" r="2" />
    <path d="M8 2v1.5M8 12.5V14M2 8h1.5M12.5 8H14M4.2 4.2l1 1M10.8 10.8l1 1M11.8 4.2l-1 1M5.2 10.8l-1 1" />
  </svg>
)

export const IconUsers = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="6" cy="5.5" r="2" />
    <path d="M2 13a4 4 0 0 1 8 0" />
    <circle cx="11.5" cy="6.5" r="1.5" />
    <path d="M10.5 13a2.5 2.5 0 0 1 3.5-2.3" />
  </svg>
)

export const IconGlobe = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="8" cy="8" r="5.5" />
    <path d="M2.5 8h11M8 2.5c-1.8 1.6-2.75 3.4-2.75 5.5S6.2 11.9 8 13.5c1.8-1.6 2.75-3.4 2.75-5.5S9.8 4.1 8 2.5Z" />
  </svg>
)

export const IconTerminal = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="1.5" y="3" width="13" height="10" rx="1.5" />
    <path d="M4 6.5 6.5 8.5 4 10.5M8 10.5h4" />
  </svg>
)

export const IconCode = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M5.5 5 2.5 8l3 3M10.5 5l3 3-3 3" />
  </svg>
)
