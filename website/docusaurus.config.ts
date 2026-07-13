import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

const config: Config = {
  title: 'Votrix Managed Agents',
  tagline: 'Open-source infrastructure for long-running agents',
  url: 'https://docs.votrix.ai',
  baseUrl: '/',

  organizationName: 'votrixai',
  projectName: 'votrix-managed-agents',

  future: {
    v4: true,
  },

  onBrokenLinks: 'throw',
  onBrokenAnchors: 'throw',
  onDuplicateRoutes: 'throw',
  markdown: {
    hooks: {
      onBrokenMarkdownLinks: 'throw',
    },
  },

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      {
        docs: {
          path: '../docs',
          routeBasePath: 'docs',
          sidebarPath: './sidebars.ts',
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    metadata: [
      {
        name: 'keywords',
        content:
          'Votrix, managed agents, long-running agents, Deep Agents, LangGraph, FastAPI',
      },
    ],
    colorMode: {
      defaultMode: 'dark',
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: 'Votrix Managed Agents',
      hideOnScroll: true,
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'docsSidebar',
          position: 'left',
          label: 'Documentation',
        },
        {
          href: 'https://managed-agents.votrix.ai/docs',
          label: 'API Reference',
          position: 'left',
        },
        {
          href: 'https://github.com/votrixai/votrix-managed-agents',
          label: 'GitHub',
          position: 'right',
        },
      ],
    },
    docs: {
      sidebar: {
        hideable: true,
      },
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Start here',
          items: [
            {
              label: 'Overview',
              to: '/docs/',
            },
            {
              label: 'Compatibility matrix',
              to: '/docs/compatibility-matrix',
            },
            {
              label: 'Known incompatibilities',
              to: '/docs/known-incompatibilities',
            },
          ],
        },
        {
          title: 'Build',
          items: [
            {
              label: 'Architecture',
              to: '/docs/votrix-core-architecture',
            },
            {
              label: 'Sandbox runtime',
              to: '/docs/sandbox-runtime',
            },
            {
              label: 'Model providers',
              to: '/docs/openai-compatible-providers',
            },
          ],
        },
        {
          title: 'Project',
          items: [
            {
              label: 'API reference',
              href: 'https://managed-agents.votrix.ai/docs',
            },
            {
              label: 'GitHub',
              href: 'https://github.com/votrixai/votrix-managed-agents',
            },
          ],
        },
      ],
      copyright:
        'Copyright © ' +
        new Date().getFullYear() +
        ' Votrix. Built with Docusaurus.',
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ['bash', 'python'],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
