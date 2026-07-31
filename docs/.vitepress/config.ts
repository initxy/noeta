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
// one complete table of contents; the top nav carries only two shortcuts +
// GitHub and must not duplicate the sidebar. Path-scoped sidebars are
// deliberately not used — they hid whole sections from readers who had not
// already guessed the URL.

// ---------------------------------------------------------------------------
// English navigation
// ---------------------------------------------------------------------------

const navEn = [
  { text: 'Quickstart', link: '/tutorials/quickstart' },
  { text: 'Reference', link: '/reference/sdk' },
  { text: 'GitHub', link: 'https://github.com/initxy/noeta' },
]

// One global sidebar — rendered identically on every page, so no section can
// hide behind a path prefix. Long groups start collapsed.
const sidebarEn = [
  {
    text: 'Tutorials',
    collapsed: false,
    items: [
      { text: 'Quickstart (5 min)', link: '/tutorials/quickstart' },
      { text: 'Your first agent', link: '/tutorials/first-agent' },
      { text: 'CI integration', link: '/tutorials/ci-integration' },
    ],
  },
  {
    text: 'How-to guides',
    collapsed: false,
    items: [
      { text: 'Configure a provider', link: '/how-to/configure-provider' },
      { text: 'Build custom tools', link: '/how-to/build-custom-tools' },
      { text: 'Spawn sub-agents', link: '/how-to/spawn-subagents' },
      { text: 'Connect MCP', link: '/how-to/connect-mcp' },
      { text: 'Write a plugin', link: '/how-to/write-a-plugin' },
      { text: 'Deploy a worker', link: '/how-to/deploy-worker' },
      { text: 'Deploy with Docker', link: '/how-to/docker-deployment' },
      { text: 'Use a sandbox', link: '/how-to/use-sandbox' },
      { text: 'Multi-tenant memory', link: '/how-to/multi-tenant-memory' },
      { text: 'Swap providers', link: '/how-to/swap-providers' },
    ],
  },
  {
    text: 'Concepts',
    collapsed: false,
    items: [
      { text: 'All concepts', link: '/concepts/' },
      { text: 'Event sourcing', link: '/concepts/event-sourcing' },
      { text: 'Task model', link: '/concepts/task-model' },
      { text: 'Engine & execution', link: '/concepts/engine-execution' },
      { text: 'Fold & snapshot', link: '/concepts/fold-and-snapshot' },
      { text: 'Wake & resume', link: '/concepts/wake-resume' },
      { text: 'Guard vs Observer', link: '/concepts/guard-observer' },
      { text: 'Composer & cache', link: '/concepts/composer-and-cache' },
      { text: 'Provider neutrality', link: '/concepts/provider-neutrality' },
    ],
  },
  {
    text: 'Architecture',
    collapsed: false,
    items: [
      { text: 'Overview', link: '/architecture/overview' },
      { text: 'Packages & boundaries', link: '/architecture/packages' },
      { text: 'State & writers', link: '/architecture/state-and-writers' },
      { text: 'Extension planes', link: '/architecture/extension-planes' },
    ],
  },
  {
    text: 'Reference',
    collapsed: true,
    items: [
      { text: 'SDK API map', link: '/reference/sdk' },
      { text: 'query / Client', link: '/reference/sdk-client' },
      { text: 'Options', link: '/reference/sdk-options' },
      { text: 'Types & testing', link: '/reference/sdk-types' },
      { text: 'Plugins overview', link: '/reference/plugins' },
      { text: 'Plugin manifest', link: '/reference/plugin-manifest' },
      { text: 'Plugin surfaces', link: '/reference/plugin-surfaces' },
      { text: 'Tools', link: '/reference/tools' },
      { text: 'Presets', link: '/reference/presets' },
      { text: 'WorkerLoop', link: '/reference/worker-loop' },
      { text: 'Comparison', link: '/reference/comparison' },
      { text: 'Glossary', link: '/reference/glossary' },
    ],
  },
  {
    text: 'Operations',
    collapsed: false,
    items: [
      { text: 'Troubleshooting', link: '/operations/troubleshooting' },
      { text: 'Known limitations', link: '/operations/limitations' },
    ],
  },
]

// ---------------------------------------------------------------------------
// Chinese navigation — same tree, /zh/ paths
// ---------------------------------------------------------------------------

const navZh = [
  { text: '快速上手', link: '/zh/tutorials/quickstart' },
  { text: '参考', link: '/zh/reference/sdk' },
  { text: 'GitHub', link: 'https://github.com/initxy/noeta' },
]

// One global sidebar for the Chinese locale — mirrors sidebarEn entry for entry.
const sidebarZh = [
  {
    text: '教程',
    collapsed: false,
    items: [
      { text: '快速上手（5 分钟）', link: '/zh/tutorials/quickstart' },
      { text: '你的第一个 agent', link: '/zh/tutorials/first-agent' },
      { text: 'CI 集成', link: '/zh/tutorials/ci-integration' },
    ],
  },
  {
    text: '操作指南',
    collapsed: false,
    items: [
      { text: '配置 Provider', link: '/zh/how-to/configure-provider' },
      { text: '构建自定义工具', link: '/zh/how-to/build-custom-tools' },
      { text: '生成子代理', link: '/zh/how-to/spawn-subagents' },
      { text: '连接 MCP', link: '/zh/how-to/connect-mcp' },
      { text: '编写插件', link: '/zh/how-to/write-a-plugin' },
      { text: '部署 Worker', link: '/zh/how-to/deploy-worker' },
      { text: '用 Docker 部署', link: '/zh/how-to/docker-deployment' },
      { text: '使用 Sandbox', link: '/zh/how-to/use-sandbox' },
      { text: '多租户记忆', link: '/zh/how-to/multi-tenant-memory' },
      { text: '切换 Provider', link: '/zh/how-to/swap-providers' },
    ],
  },
  {
    text: '核心概念',
    collapsed: false,
    items: [
      { text: '概念总览', link: '/zh/concepts/' },
      { text: '事件溯源', link: '/zh/concepts/event-sourcing' },
      { text: '任务模型', link: '/zh/concepts/task-model' },
      { text: '引擎与执行', link: '/zh/concepts/engine-execution' },
      { text: 'Fold 与快照', link: '/zh/concepts/fold-and-snapshot' },
      { text: '唤醒与恢复', link: '/zh/concepts/wake-resume' },
      { text: 'Guard 与 Observer', link: '/zh/concepts/guard-observer' },
      { text: 'Composer 与缓存', link: '/zh/concepts/composer-and-cache' },
      { text: 'Provider 中立', link: '/zh/concepts/provider-neutrality' },
    ],
  },
  {
    text: '架构',
    collapsed: false,
    items: [
      { text: '概览', link: '/zh/architecture/overview' },
      { text: '包与导入规则', link: '/zh/architecture/packages' },
      { text: '状态与写入者', link: '/zh/architecture/state-and-writers' },
      { text: '扩展平面', link: '/zh/architecture/extension-planes' },
    ],
  },
  {
    text: '参考',
    collapsed: true,
    items: [
      { text: 'SDK API 地图', link: '/zh/reference/sdk' },
      { text: 'query / Client', link: '/zh/reference/sdk-client' },
      { text: 'Options', link: '/zh/reference/sdk-options' },
      { text: '类型与测试替身', link: '/zh/reference/sdk-types' },
      { text: '插件总览', link: '/zh/reference/plugins' },
      { text: '插件 manifest', link: '/zh/reference/plugin-manifest' },
      { text: '插件 Surface', link: '/zh/reference/plugin-surfaces' },
      { text: '工具', link: '/zh/reference/tools' },
      { text: '预设代理', link: '/zh/reference/presets' },
      { text: 'WorkerLoop', link: '/zh/reference/worker-loop' },
      { text: '对比', link: '/zh/reference/comparison' },
      { text: '术语表', link: '/zh/reference/glossary' },
    ],
  },
  {
    text: '运维',
    collapsed: false,
    items: [
      { text: '故障排查', link: '/zh/operations/troubleshooting' },
      { text: '已知限制', link: '/zh/operations/limitations' },
    ],
  },
]

export default defineConfig({
  title: 'Noeta',
  description: 'Open-source, self-hostable runtime for AI agents.',

  // GitHub Pages subpath.
  base: '/noeta/',

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
    // GitHub link in nav.
    nav: [
      { text: 'GitHub', link: 'https://github.com/initxy/noeta' },
    ],

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
