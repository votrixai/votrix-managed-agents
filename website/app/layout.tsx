import type { Metadata } from 'next';
import { Provider } from '@/components/provider';
import { GeistMono } from 'geist/font/mono';
import '@fontsource/dm-sans/latin-300.css';
import '@fontsource/dm-sans/latin-300-italic.css';
import '@fontsource/dm-sans/latin-400.css';
import '@fontsource/dm-sans/latin-400-italic.css';
import '@fontsource/dm-sans/latin-500.css';
import '@fontsource/dm-sans/latin-500-italic.css';
import '@fontsource/dm-sans/latin-600.css';
import '@fontsource/dm-sans/latin-600-italic.css';
import '@fontsource/dm-sans/latin-700.css';
import '@fontsource/dm-sans/latin-700-italic.css';
import './global.css';

export const metadata: Metadata = {
  metadataBase: new URL('https://docs.votrixai.com'),
  title: {
    default: 'Votrix Managed Agents',
    template: '%s | Votrix Managed Agents',
  },
  description:
    'Open-source infrastructure and API reference for long-running managed agents.',
  robots: {
    index: false,
    follow: false,
    googleBot: {
      index: false,
      follow: false,
      noarchive: true,
      nosnippet: true,
    },
  },
  keywords: [
    'Votrix',
    'managed agents',
    'long-running agents',
    'Deep Agents',
    'LangGraph',
    'FastAPI',
  ],
};

export default function RootLayout({ children }: LayoutProps<'/'>) {
  return (
    <html lang="en" className={GeistMono.variable} suppressHydrationWarning>
      <body className="flex min-h-screen flex-col antialiased">
        <Provider>{children}</Provider>
      </body>
    </html>
  );
}
