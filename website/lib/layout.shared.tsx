import type { BaseLayoutProps } from 'fumadocs-ui/layouts/shared';
import Image from 'next/image';
import { appName } from '@/lib/shared';

function VotrixMark() {
  return (
    <span className="votrix-mark" aria-hidden="true">
      <Image src="/logo.png" alt="" width={32} height={32} priority />
    </span>
  );
}

export function baseOptions(): BaseLayoutProps {
  return {
    nav: {
      title: (
        <span className="flex items-center gap-2.5 font-semibold tracking-[-0.02em]">
          <VotrixMark />
          <span>{appName}</span>
        </span>
      ),
    },
    links: [
      {
        text: 'Overview',
        url: '/docs',
        active: 'url',
        on: 'nav',
      },
      {
        type: 'button',
        text: 'API Reference',
        url: '/docs/api',
        active: 'nested-url',
        on: 'nav',
      },
    ],
  };
}
