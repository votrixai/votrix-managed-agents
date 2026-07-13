import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  docsSidebar: [
    'index',
    {
      type: 'category',
      label: 'Compatibility',
      collapsed: false,
      items: [
        'compatibility-matrix',
        'known-incompatibilities',
        'claude-managed-agents-alignment',
        'managed-agents-api-coverage',
        'anthropic-sdk-contract-tests',
      ],
    },
    {
      type: 'category',
      label: 'Architecture & runtime',
      items: [
        'votrix-core-architecture',
        'agent-versioning',
        'openai-compatible-providers',
        'sandbox-runtime',
        'memory-stores',
      ],
    },
    {
      type: 'category',
      label: 'Operations',
      items: [
        'work-queue',
        {
          type: 'doc',
          id: 'deployments',
          label: 'Scheduled deployments',
        },
        'webhooks',
        'deployment-platforms',
      ],
    },
  ],
};

export default sidebars;
