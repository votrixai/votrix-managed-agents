import Link from 'next/link';
import {
  ArrowRight,
  BookOpen,
  Boxes,
  Braces,
  Check,
  GitBranch,
  ServerCog,
} from 'lucide-react';

const paths = [
  {
    icon: Braces,
    eyebrow: 'Evaluate',
    title: 'Explore the API',
    description:
      'Inspect every endpoint, fill request fields, authenticate, and send real HTTP requests.',
    href: '/docs/api',
  },
  {
    icon: GitBranch,
    eyebrow: 'Build',
    title: 'Understand the runtime',
    description:
      'Follow the control plane, persistence, provider, and sandbox boundaries.',
    href: '/docs/votrix-core-architecture',
  },
  {
    icon: ServerCog,
    eyebrow: 'Operate',
    title: 'Deploy with confidence',
    description:
      'Review the supported Cloud Run topology and current production constraints.',
    href: '/docs/deployment-platforms',
  },
] as const;

export default function HomePage() {
  return (
    <main className="relative flex-1 overflow-hidden">
      <div className="home-grid pointer-events-none absolute inset-x-0 top-0 h-[760px]" />
      <div className="hero-glow pointer-events-none absolute left-1/2 top-[-18rem] h-[46rem] w-[60rem] -translate-x-1/2" />

      <section className="relative mx-auto grid w-full max-w-[1240px] gap-14 px-6 pb-20 pt-24 lg:grid-cols-[1.1fr_0.9fr] lg:items-center lg:px-10 lg:pb-28 lg:pt-32">
        <div>
          <div className="mb-6 inline-flex items-center gap-2 rounded-full border bg-fd-card/75 px-3 py-1.5 text-xs font-medium text-fd-muted-foreground shadow-sm backdrop-blur">
            <span className="h-1.5 w-1.5 rounded-full bg-[var(--votrix-brand)]" />
            Open source · Self hosted · Multi tenant
          </div>
          <h1 className="max-w-[760px] text-balance text-5xl font-semibold leading-[1.02] tracking-[-0.055em] text-fd-foreground sm:text-6xl lg:text-7xl">
            Run long-lived agents on infrastructure you control.
          </h1>
          <p className="mt-7 max-w-[680px] text-pretty text-lg leading-8 text-fd-muted-foreground sm:text-xl">
            A durable, Organization-scoped control plane aligned with the public Claude Managed
            Agents resource and SDK shape, powered by Deep Agents and LangGraph.
          </p>
          <div className="mt-9 flex flex-wrap items-center gap-3">
            <Link
              href="/docs"
              className="inline-flex h-11 items-center gap-2 rounded-lg bg-fd-primary px-5 text-sm font-semibold text-fd-primary-foreground shadow-lg shadow-[rgba(26,25,23,0.12)] transition hover:opacity-90"
            >
              Read the docs
              <ArrowRight className="size-4" />
            </Link>
            <Link
              href="/docs/api"
              className="inline-flex h-11 items-center gap-2 rounded-lg border bg-fd-background/75 px-5 text-sm font-semibold text-fd-foreground shadow-sm backdrop-blur transition hover:bg-fd-accent"
            >
              <BookOpen className="size-4" />
              API reference
            </Link>
          </div>
          <div className="mt-7 flex flex-wrap gap-x-5 gap-y-2 text-xs font-medium text-fd-muted-foreground">
            {['FastAPI', 'Deep Agents', 'LangGraph', 'Postgres'].map((item) => (
              <span key={item} className="inline-flex items-center gap-1.5">
                <Check className="size-3.5 text-[var(--votrix-brand)]" />
                {item}
              </span>
            ))}
          </div>
        </div>

        <div className="terminal-window relative overflow-hidden rounded-2xl border border-white/10 bg-[#1A1917] text-[#FAF9F7]">
          <div className="flex h-12 items-center gap-2 border-b border-white/8 bg-[#23221F] px-4">
            <span className="size-2.5 rounded-full bg-[#ff6c72]" />
            <span className="size-2.5 rounded-full bg-[#f4c452]" />
            <span className="size-2.5 rounded-full bg-[#63d786]" />
            <span className="ml-auto font-mono text-[11px] text-[#AAA79F]">local setup</span>
          </div>
          <div className="space-y-4 p-6 font-mono text-[13px] leading-6 sm:p-7">
            {[
              'cp .env.example .env',
              'uv sync',
              'uv run alembic upgrade head',
              './run.sh',
            ].map((command) => (
              <div key={command} className="flex gap-3">
                <span className="select-none text-[#83B69B]">$</span>
                <span>{command}</span>
              </div>
            ))}
            <div className="mt-5 flex items-center gap-3 border-t border-white/8 pt-5 text-[#83B69B]">
              <Check className="size-4" />
              <span>API ready on :8080</span>
            </div>
          </div>
          <div className="absolute -bottom-20 -right-20 size-52 rounded-full bg-[rgba(45,106,79,0.22)] blur-3xl" />
        </div>
      </section>

      <section className="relative border-y bg-fd-muted/35">
        <div className="mx-auto flex max-w-[1240px] flex-col gap-4 px-6 py-6 sm:flex-row sm:items-center lg:px-10">
          <span className="w-fit rounded-full bg-[var(--votrix-brand-light)] px-2.5 py-1 text-[11px] font-bold uppercase tracking-[0.1em] text-[var(--votrix-brand)]">
            Project status
          </span>
          <p className="flex-1 text-sm leading-6 text-fd-muted-foreground">
            VMA is an early 0.1.0 project. Its REST surface is broader than its production
            execution surface.
          </p>
          <Link
            href="/docs/known-incompatibilities"
            className="inline-flex items-center gap-1.5 text-sm font-semibold text-fd-foreground hover:text-fd-primary"
          >
            Review the gaps <ArrowRight className="size-3.5" />
          </Link>
        </div>
      </section>

      <section className="mx-auto w-full max-w-[1240px] px-6 py-20 lg:px-10 lg:py-28">
        <div className="mb-10 max-w-2xl">
          <div className="mb-3 inline-flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.13em] text-fd-primary">
            <Boxes className="size-4" /> Choose your path
          </div>
          <h2 className="text-3xl font-semibold tracking-[-0.04em] text-fd-foreground sm:text-4xl">
            Start with the layer you own.
          </h2>
        </div>
        <div className="grid gap-4 md:grid-cols-3">
          {paths.map(({ icon: Icon, eyebrow, title, description, href }) => (
            <Link
              key={title}
              href={href}
              className="group flex min-h-64 flex-col rounded-2xl border bg-fd-card p-6 shadow-sm transition duration-200 hover:-translate-y-1 hover:border-[color:var(--votrix-brand)]/40 hover:shadow-xl hover:shadow-[rgba(26,25,23,0.08)]"
            >
              <div className="mb-8 grid size-10 place-items-center rounded-xl bg-[var(--votrix-accent-soft)] text-fd-primary">
                <Icon className="size-5" />
              </div>
              <span className="text-[11px] font-bold uppercase tracking-[0.12em] text-fd-primary">
                {eyebrow}
              </span>
              <h3 className="mt-2 text-lg font-semibold tracking-[-0.025em] text-fd-foreground">
                {title}
              </h3>
              <p className="mt-3 text-sm leading-6 text-fd-muted-foreground">{description}</p>
              <span className="mt-auto inline-flex items-center gap-1.5 pt-6 text-sm font-semibold text-fd-foreground transition group-hover:text-fd-primary">
                Learn more <ArrowRight className="size-3.5 transition group-hover:translate-x-0.5" />
              </span>
            </Link>
          ))}
        </div>
      </section>
    </main>
  );
}
