import { defineConfig } from 'vitepress'

// VitePress config for Noeta docs.
//
// Build:   npm run docs:build
// Dev:     npm run docs:dev
// Deploy:  GitHub Actions (.github/workflows/docs.yml) on push to main
//
// Served from /noeta/ subpath on GitHub Pages.
// i18n: English at /noeta/, Chinese at /noeta/zh/
//
// Navigation contract: the single global sidebar (same on every page) is the
// one complete table of contents; the top nav carries only shortcuts (GitHub
// lives in the social links) and must not duplicate the sidebar. Path-scoped sidebars are
// deliberately not used — they hid whole sections from readers who had not
// already guessed the URL.

// ---------------------------------------------------------------------------
// English navigation
// ---------------------------------------------------------------------------

const navEn = [
  { text: 'Why Noeta', link: '/why-noeta' },
  { text: 'Quickstart', link: '/start/quickstart' },
  { text: 'Reference', link: '/reference/sdk' },
]

// One global sidebar, ordered the way a reader goes: decide, start, build,
// understand, look up, operate.
const sidebarEn = [
  {
    text: 'Get started',
    items: [
      { text: 'Why Noeta', link: '/why-noeta' },
      { text: 'Quickstart', link: '/start/quickstart' },
      { text: 'Tutorial: build an agent', link: '/start/tutorial' },
    ],
  },
  {
    text: 'Guides',
    items: [
      { text: 'Connect a model', link: '/guides/models' },
      { text: 'Custom tools', link: '/guides/tools' },
      { text: 'MCP servers', link: '/guides/mcp' },
      { text: 'Subagents', link: '/guides/subagents' },
      { text: 'Write a plugin', link: '/guides/plugins' },
      { text: 'Test offline & in CI', link: '/guides/testing' },
      { text: 'Deploy to production', link: '/guides/deploy' },
      { text: 'Run tools in a sandbox', link: '/guides/sandbox' },
      { text: 'Per-tenant memory', link: '/guides/multi-tenant-memory' },
    ],
  },
  {
    text: 'How it works',
    items: [
      { text: 'Overview', link: '/how-it-works/' },
      { text: 'Event log & recovery', link: '/how-it-works/event-log' },
      { text: 'Tasks & waking', link: '/how-it-works/tasks-and-waking' },
      { text: 'The engine loop', link: '/how-it-works/engine' },
      { text: 'Context & caching', link: '/how-it-works/context' },
      { text: 'Plugin system', link: '/how-it-works/plugin-system' },
    ],
  },
  {
    text: 'Reference',
    collapsed: true,
    items: [
      { text: 'SDK: query & Client', link: '/reference/sdk' },
      { text: 'Options', link: '/reference/options' },
      { text: 'Types & test doubles', link: '/reference/types' },
      { text: 'Built-in tools', link: '/reference/tools' },
      { text: 'Presets', link: '/reference/presets' },
      { text: 'Plugin manifest', link: '/reference/plugin-manifest' },
      { text: 'Plugin surfaces', link: '/reference/plugin-surfaces' },
      { text: 'WorkerLoop', link: '/reference/worker-loop' },
      { text: 'Glossary', link: '/reference/glossary' },
    ],
  },
  {
    text: 'Operations',
    items: [
      { text: 'Troubleshooting', link: '/operations/troubleshooting' },
      { text: 'Known limitations', link: '/operations/limitations' },
      { text: 'Benchmarks', link: '/benchmarks' },
    ],
  },
]

// ---------------------------------------------------------------------------
// Chinese navigation — same tree, /zh/ paths
// ---------------------------------------------------------------------------

const navZh = [
  { text: '为什么选 Noeta', link: '/zh/why-noeta' },
  { text: '快速上手', link: '/zh/start/quickstart' },
  { text: '参考', link: '/zh/reference/sdk' },
]

const sidebarZh = [
  {
    text: '入门',
    items: [
      { text: '为什么选 Noeta', link: '/zh/why-noeta' },
      { text: '快速上手', link: '/zh/start/quickstart' },
      { text: '教程：搭一个完整的 agent', link: '/zh/start/tutorial' },
    ],
  },
  {
    text: '使用指南',
    items: [
      { text: '接入模型', link: '/zh/guides/models' },
      { text: '自定义工具', link: '/zh/guides/tools' },
      { text: '接入 MCP', link: '/zh/guides/mcp' },
      { text: '子代理', link: '/zh/guides/subagents' },
      { text: '写一个插件', link: '/zh/guides/plugins' },
      { text: '离线测试与 CI', link: '/zh/guides/testing' },
      { text: '部署上线', link: '/zh/guides/deploy' },
      { text: '在沙箱里跑工具', link: '/zh/guides/sandbox' },
      { text: '按租户隔离记忆', link: '/zh/guides/multi-tenant-memory' },
    ],
  },
  {
    text: '原理',
    items: [
      { text: '总览', link: '/zh/how-it-works/' },
      { text: '事件日志与故障恢复', link: '/zh/how-it-works/event-log' },
      { text: '任务与唤醒', link: '/zh/how-it-works/tasks-and-waking' },
      { text: '引擎主循环', link: '/zh/how-it-works/engine' },
      { text: '上下文与缓存', link: '/zh/how-it-works/context' },
      { text: '插件系统', link: '/zh/how-it-works/plugin-system' },
    ],
  },
  {
    text: '参考',
    collapsed: true,
    items: [
      { text: 'SDK：query 与 Client', link: '/zh/reference/sdk' },
      { text: 'Options', link: '/zh/reference/options' },
      { text: '类型与测试替身', link: '/zh/reference/types' },
      { text: '内置工具', link: '/zh/reference/tools' },
      { text: '预设 agent', link: '/zh/reference/presets' },
      { text: '插件 manifest', link: '/zh/reference/plugin-manifest' },
      { text: '插件扩展点', link: '/zh/reference/plugin-surfaces' },
      { text: 'WorkerLoop', link: '/zh/reference/worker-loop' },
      { text: '术语表', link: '/zh/reference/glossary' },
    ],
  },
  {
    text: '运维',
    items: [
      { text: '故障排查', link: '/zh/operations/troubleshooting' },
      { text: '已知限制', link: '/zh/operations/limitations' },
      { text: '基准测试', link: '/zh/benchmarks' },
    ],
  },
]

export default defineConfig({
  title: 'Noeta',
  description: 'A Python SDK for agents that survive crashes, wait for days, and scale from a script to a cluster.',

  // GitHub Pages subpath.
  base: '/noeta/',

  // Favicon (the icon lives in docs/public/, served at the base root).
  head: [
    ['link', { rel: 'icon', type: 'image/svg+xml', href: '/noeta/logo.svg' }],
  ],

  // Dead-link checking is ON (VitePress default): a broken internal link
  // fails the build. Pages excluded from the site (see srcExclude — ADRs and
  // implementation specs) are referenced only via absolute GitHub source URLs,
  // so nothing internal points at a non-published page.
  ignoreDeadLinks: false,

  // Ignore internal docs from the build — they stay in the repo for
  // contributors but are not published to the public site.
  srcExclude: [
    '**/adr/**',
    '**/implementation-specs/**',
    '**/reference/api/**',
    'releasing.md',
    'releasing.zh.md',
  ],

  themeConfig: {
    // Brand mark in the top-left of the nav.
    logo: '/logo.svg',

    // Social links in footer.
    socialLinks: [
      { icon: 'github', link: 'https://github.com/initxy/noeta' },
    ],

    // Footer.
    footer: {
      message: 'Released under the Apache License 2.0.',
      copyright: 'Copyright &copy; 2025–2026 Noeta Contributors',
    },

    // Search — built-in local search (no external service needed).
    search: {
      provider: 'local',
    },

    // Show "Edit this page" link.
    editLink: {
      pattern: 'https://github.com/initxy/noeta/edit/main/docs/:path',
      text: 'Edit this page on GitHub',
    },

    // Return-to-top button.
    returnToTopLabel: 'Back to top',

    // Sidebar label for outline (right-side TOC).
    outline: {
      label: 'On this page',
      level: [2, 3],
    },

    // Last updated text.
    lastUpdated: {
      text: 'Last updated',
      formatOptions: { dateStyle: 'medium' },
    },

    // Dark / light mode toggle label.
    darkModeSwitchLabel: 'Appearance',
    lightModeSwitchTitle: 'Switch to light mode',
    darkModeSwitchTitle: 'Switch to dark mode',

    // Sidebar menu label (mobile).
    sidebarMenuLabel: 'Menu',
  },

  // -----------------------------------------------------------------------
  // i18n — English (default) + Chinese
  // -----------------------------------------------------------------------
  locales: {
    root: {
      label: 'English',
      lang: 'en',
      themeConfig: {
        nav: navEn,
        sidebar: sidebarEn,
      },
    },

    zh: {
      label: '中文',
      lang: 'zh-CN',
      link: '/zh/',
      description: '一个 Python SDK：agent 崩了能接着跑，等人审批不占资源，从脚本扩到多机集群不用改代码。',
      themeConfig: {
        nav: navZh,
        sidebar: sidebarZh,

        // Chinese-specific theme labels.
        returnToTopLabel: '返回顶部',
        outline: { label: '本页目录', level: [2, 3] },
        lastUpdated: { text: '最后更新', formatOptions: { dateStyle: 'medium' } },
        darkModeSwitchLabel: '外观',
        lightModeSwitchTitle: '切换到浅色模式',
        darkModeSwitchTitle: '切换到深色模式',
        sidebarMenuLabel: '菜单',
        footer: {
          message: '基于 Apache License 2.0 发布。',
          copyright: 'Copyright &copy; 2025–2026 Noeta Contributors',
        },
        docFooter: {
          prev: '上一页',
          next: '下一页',
        },
        editLink: {
          pattern: 'https://github.com/initxy/noeta/edit/main/docs/:path',
          text: '在 GitHub 上编辑此页',
        },
      },
    },
  },
})
