import { defineConfig } from 'vitepress'
import cjkFriendly from 'markdown-it-cjk-friendly'

// The noeta docs site. English at /noeta/, Chinese at /noeta/zh/ (same tree, same file names).
// Dev: `npm run dev` in docs/. Build: `npm run build`. Deploy: .github/workflows/docs.yml.
// One global sidebar per language is the whole table of contents; the top nav only has
// shortcuts. ADRs, specs and the maintainer pages stay in the repository, not on the site.

type Item = { text: string; link: string }
type Group = { text: string; collapsed?: boolean; items: Item[] }

// [path, English title, Chinese title], grouped. A page's file is docs/<path>.md and
// docs/zh/<path>.md.
const tree: [string, string, [string, string, string][]][] = [
  ['Get started', '入门', [
    ['/why-noeta', 'Why noeta', '为什么是 noeta'],
    ['/start/quickstart', 'Quickstart', '快速上手'],
    ['/start/configuration', 'Configuration', '配置'],
    ['/start/embedding', 'Embed it in Python', '在 Python 里嵌入'],
  ]],
  ['Guides', '使用指南', [
    ['/guides/terminal', 'The terminal UI and -p', '终端界面与 -p'],
    ['/guides/front-ends', 'Web, desktop and editors', '网页、桌面与编辑器'],
    ['/guides/models', 'Models and providers', '模型与 provider'],
    ['/guides/permissions', 'Permissions and hooks', '权限与 hooks'],
    ['/guides/skills-and-agents', 'Skills, agents and commands', '技能、代理与命令'],
    ['/guides/subagents', 'Sub-agents and background work', '子代理与后台任务'],
    ['/guides/mcp', 'MCP servers', 'MCP 服务器'],
    ['/guides/memory-and-instructions', 'Instructions and memory', '项目说明与记忆'],
    ['/guides/plugins', 'Write a plugin', '写一个插件'],
    ['/guides/server', 'Run it as a server', '作为服务端运行'],
    ['/guides/docker', 'Deploy with Docker', '用 Docker 部署'],
    ['/guides/sandbox', 'Where commands run', '命令在哪里执行'],
  ]],
  ['How it works', '设计与原理', [
    ['/how-it-works/', 'Overview', '总览'],
    ['/how-it-works/event-log', 'A task is a log', '任务就是一条日志'],
    ['/how-it-works/one-step', 'One step of a task', '任务的一步'],
    ['/how-it-works/waiting', 'Waiting and waking', '等待与唤醒'],
    ['/how-it-works/recovery', 'One writer and crash recovery', '单写者与崩溃恢复'],
    ['/how-it-works/context', 'Context, caching and compaction', '上下文、缓存与压缩'],
    ['/how-it-works/plugins', 'The plugin system', '插件体系'],
    ['/how-it-works/models', 'Providers and model routing', 'provider 与模型路由'],
    ['/how-it-works/many-machines', 'Many machines', '多机运行'],
    ['/how-it-works/parity', 'Matching Claude Code', '如何对齐 Claude Code'],
  ]],
  ['Reference', '参考', [
    ['/reference/cli', 'The noeta command', 'noeta 命令'],
    ['/reference/config', 'Configuration keys', '配置项'],
    ['/reference/tools', 'Built-in tools', '内置工具'],
    ['/reference/plugins', 'Built-in plugins', '内置插件'],
    ['/reference/extension', 'Extension interface', 'Extension 接口'],
    ['/reference/events', 'Events', '事件'],
    ['/reference/http-api', 'HTTP / WebSocket API', 'HTTP / WebSocket 接口'],
    ['/reference/python-api', 'Python API', 'Python 接口'],
  ]],
  ['Operations', '运维', [
    ['/operations/troubleshooting', 'Troubleshooting', '排错'],
    ['/operations/limitations', 'Known limitations', '已知限制'],
    ['/operations/comparison', 'Compared with other agents', '和其他 agent 对比'],
    ['/benchmarks', 'Benchmarks', '评测'],
  ]],
]

const sidebar = (zh: boolean): Group[] =>
  tree.map(([en, cn, items]) => ({
    text: zh ? cn : en,
    items: items.map(([path, t, c]) => ({ text: zh ? c : t, link: (zh ? '/zh' : '') + path })),
  }))

const nav = (zh: boolean) => {
  const p = zh ? '/zh' : ''
  return [
    { text: zh ? '快速上手' : 'Quickstart', link: `${p}/start/quickstart` },
    { text: zh ? '设计与原理' : 'How it works', link: `${p}/how-it-works/` },
    { text: zh ? '参考' : 'Reference', link: `${p}/reference/cli` },
  ]
}

export default defineConfig({
  title: 'noeta',
  description: 'The open-source, Claude Code-compatible coding agent you can self-host on many workers: one Postgres store, any worker continues any task, waiting holds no process.',
  base: '/noeta/',
  cleanUrls: true,
  lastUpdated: true,
  srcExclude: ['adr/**', 'specs/**', 'gc.md', 'releasing.md', 'reviewing.md', 'README.md'],
  head: [
    ['link', { rel: 'icon', type: 'image/svg+xml', href: '/noeta/logo.svg' }],
    ['meta', { property: 'og:image', content: 'https://initxy.github.io/noeta/og.png' }],
    ['meta', { name: 'twitter:card', content: 'summary_large_image' }],
  ],
  // CommonMark drops **bold** whose delimiter sits between CJK punctuation and a character
  // (`**做决定。**如果`); this plugin applies the CJK-friendly emphasis rules.
  markdown: { config: (md) => { md.use(cjkFriendly) } },
  // <Diagram> inlines every docs/diagrams/*.svg into the theme bundle (~90 KB gzipped, cached).
  vite: { build: { chunkSizeWarningLimit: 1000 } },
  themeConfig: {
    logo: '/logo.svg',
    socialLinks: [{ icon: 'github', link: 'https://github.com/initxy/noeta' }],
    search: { provider: 'local' },
  },
  locales: {
    root: {
      label: 'English',
      lang: 'en-US',
      themeConfig: {
        nav: nav(false),
        sidebar: sidebar(false),
        editLink: { pattern: 'https://github.com/initxy/noeta/edit/main/docs/:path' },
      },
    },
    zh: {
      label: '简体中文',
      lang: 'zh-CN',
      link: '/zh/',
      themeConfig: {
        nav: nav(true),
        sidebar: sidebar(true),
        outline: { label: '本页内容' },
        docFooter: { prev: '上一页', next: '下一页' },
        lastUpdated: { text: '最后更新' },
        editLink: { pattern: 'https://github.com/initxy/noeta/edit/main/docs/:path', text: '在 GitHub 上编辑' },
        returnToTopLabel: '回到顶部',
        sidebarMenuLabel: '目录',
        darkModeSwitchLabel: '外观',
      },
    },
  },
})
