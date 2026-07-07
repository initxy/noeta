import { defineConfig } from 'vitepress'

// VitePress config for Noeta docs.
//
// Build:   npm run docs:build
// Dev:     npm run docs:dev
// Deploy:  GitHub Actions (.github/workflows/docs.yml) on push to main
//
// Served from /noeta/ subpath on GitHub Pages.
// i18n: English at /noeta/, Chinese at /noeta/zh/

export default defineConfig({
  title: 'Noeta',
  description: 'Open-source, self-hostable runtime for AI agents.',

  // GitHub Pages subpath.
  base: '/noeta/',

  // Dead-link checking is ON (VitePress default): a broken internal link
  // fails the build. Pages excluded from the site (see srcExclude — ADRs,
  // implementation specs, drafts) are referenced only via absolute GitHub
  // source URLs, so nothing internal points at a non-published page.
  ignoreDeadLinks: false,

  // Ignore internal docs from the build — they stay in the repo for
  // contributors but are not published to the public site.
  srcExclude: [
    '**/adr/**',
    '**/implementation-specs/**',
    '**/reference/api/**',
    '**/_drafts/**',
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
      message: 'Released under the MIT License.',
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
        nav: [
          { text: 'Guide', link: '/tutorials/quickstart' },
          { text: 'API', link: '/reference/sdk' },
          { text: 'GitHub', link: 'https://github.com/initxy/noeta' },
        ],

        sidebar: {
          '/tutorials/': [
            {
              text: 'Tutorials',
              items: [
                { text: 'Quickstart', link: '/tutorials/quickstart' },
                { text: 'Your first agent', link: '/tutorials/first-agent' },
                { text: 'Build a research agent', link: '/tutorials/build-a-research-agent' },
                { text: 'CI integration', link: '/tutorials/ci-integration' },
              ],
            },
          ],

          '/how-to/': [
            {
              text: 'How-to guides',
              items: [
                { text: 'Configure a provider', link: '/how-to/configure-provider' },
                { text: 'Use the coding agent', link: '/how-to/use-the-coding-agent' },
                { text: 'Build custom tools', link: '/how-to/build-custom-tools' },
                { text: 'Spawn sub-agents', link: '/how-to/spawn-subagents' },
                { text: 'Connect MCP', link: '/how-to/connect-mcp' },
                { text: 'Deploy a worker', link: '/how-to/deploy-worker' },
                { text: 'Swap providers', link: '/how-to/swap-providers' },
              ],
            },
          ],

          '/concepts/': [
            {
              text: 'Concepts',
              items: [
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
          ],

          '/reference/': [
            {
              text: 'Reference',
              items: [
                { text: 'SDK API', link: '/reference/sdk' },
                { text: 'Coding agent', link: '/reference/noeta-agent' },
                { text: 'HTTP API', link: '/reference/http-api' },
                { text: 'WorkerLoop', link: '/reference/worker-loop' },
                { text: 'Comparison', link: '/reference/comparison' },
                { text: 'Configuration', link: '/reference/configuration' },
                { text: 'Tools', link: '/reference/tools' },
                { text: 'Presets', link: '/reference/presets' },
                { text: 'Glossary', link: '/reference/glossary' },
              ],
            },
          ],

          '/architecture/': [
            {
              text: 'Architecture',
              items: [
                { text: 'Overview', link: '/architecture/overview' },
              ],
            },
          ],

          '/operations/': [
            {
              text: 'Operations',
              items: [
                { text: 'Troubleshooting', link: '/operations/troubleshooting' },
                { text: 'Known limitations', link: '/operations/limitations' },
              ],
            },
          ],
        },
      },
    },

    zh: {
      label: '中文',
      lang: 'zh-CN',
      link: '/zh/',
      themeConfig: {
        nav: [
          { text: '指南', link: '/zh/tutorials/build-a-research-agent' },
          { text: 'API', link: '/zh/reference/tools' },
          { text: 'GitHub', link: 'https://github.com/initxy/noeta' },
        ],

        sidebar: {
          '/zh/tutorials/': [
            {
              text: '教程',
              items: [
                { text: '构建研究代理', link: '/zh/tutorials/build-a-research-agent' },
                { text: 'CI 集成', link: '/zh/tutorials/ci-integration' },
              ],
            },
          ],

          '/zh/reference/': [
            {
              text: '参考',
              items: [
                { text: '配置', link: '/zh/reference/configuration' },
                { text: '工具', link: '/zh/reference/tools' },
                { text: '预设代理', link: '/zh/reference/presets' },
                { text: 'HTTP 接口', link: '/zh/reference/http-api' },
                { text: '术语表', link: '/zh/reference/glossary' },
              ],
            },
          ],
        },

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
