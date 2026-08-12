import Link from 'next/link';
import {
  ArrowRight,
  BookOpen,
  Bot,
  Brain,
  Braces,
  FolderOpen,
  Workflow,
} from 'lucide-react';

const capabilities = [
  {
    icon: Bot,
    eyebrow: 'Create',
    title: 'Agents built for real work',
    description:
      'Give each agent a clear role, instructions, tools, and reusable skills for the work it needs to do.',
    href: '/docs/api',
  },
  {
    icon: Workflow,
    eyebrow: 'Continue',
    title: 'Work that carries forward',
    description:
      'Keep a session going across multiple interactions so the agent can pick up where it left off.',
    href: '/docs/session-events',
  },
  {
    icon: Brain,
    eyebrow: 'Remember',
    title: 'Context that stays useful',
    description:
      'Keep files and shared memory close to the work, so important context is available when it is needed.',
    href: '/docs/memory-stores',
  },
] as const;

const workflow = [
  {
    title: 'Define the agent',
    description: 'Choose its role, instructions, tools, and knowledge.',
  },
  {
    title: 'Give it a task',
    description: 'Start a session with the files and context it needs.',
  },
  {
    title: 'Follow the work',
    description: 'See progress as it happens, then continue whenever you need to.',
  },
] as const;

export default function HomePage() {
  return (
    <main className="relative flex-1 overflow-hidden">
      <div className="home-grid pointer-events-none absolute inset-x-0 top-0 h-[760px]" />
      <div className="hero-glow pointer-events-none absolute left-1/2 top-[-18rem] h-[46rem] w-[60rem] -translate-x-1/2" />

      <section className="relative mx-auto grid w-full max-w-[1240px] gap-14 px-6 pb-20 pt-24 lg:grid-cols-[1.08fr_0.92fr] lg:items-center lg:px-10 lg:pb-28 lg:pt-32">
        <div>
          <div className="mb-6 inline-flex items-center gap-2 rounded-full border bg-fd-card/75 px-3 py-1.5 text-xs font-medium text-fd-muted-foreground shadow-sm backdrop-blur">
            <span className="h-1.5 w-1.5 rounded-full bg-[var(--votrix-brand)]" />
            Agent harness with built-in sandboxes
          </div>
          <h1 className="max-w-[760px] text-balance text-5xl font-semibold leading-[1.02] tracking-[-0.055em] text-fd-foreground sm:text-6xl lg:text-7xl">
            Give every agent a place to do real work.
          </h1>
          <p className="mt-7 max-w-[680px] text-pretty text-lg leading-8 text-fd-muted-foreground sm:text-xl">
            VMA is an API-first agent harness framework. It gives every agent a sandbox for
            tools, files, and multi-step work, while keeping its memory and progress together.
          </p>
          <div className="mt-9 flex flex-wrap items-center gap-3">
            <Link
              href="/docs/api"
              className="inline-flex h-11 items-center gap-2 rounded-lg bg-fd-primary px-5 text-sm font-semibold text-fd-primary-foreground shadow-lg shadow-[rgba(26,25,23,0.12)] transition hover:opacity-90"
            >
              <Braces className="size-4" />
              API Reference
              <ArrowRight className="size-4" />
            </Link>
            <Link
              href="/docs"
              className="inline-flex h-11 items-center gap-2 rounded-lg border bg-fd-background/75 px-5 text-sm font-semibold text-fd-foreground shadow-sm backdrop-blur transition hover:bg-fd-accent"
            >
              <BookOpen className="size-4" />
              Overview
            </Link>
          </div>
        </div>

        <div className="relative overflow-hidden rounded-3xl border bg-fd-card/90 p-6 shadow-2xl shadow-[rgba(26,25,23,0.1)] backdrop-blur sm:p-8">
          <div className="mb-8 flex items-start gap-4">
            <div className="grid size-11 flex-none place-items-center rounded-xl bg-[var(--votrix-accent-soft)] text-fd-primary">
              <FolderOpen className="size-5" />
            </div>
            <div>
              <p className="text-xs font-bold uppercase tracking-[0.12em] text-fd-primary">
                One continuous sandbox
              </p>
              <h2 className="mt-1.5 text-xl font-semibold tracking-[-0.03em] text-fd-foreground">
                From request to finished work
              </h2>
            </div>
          </div>

          <div className="space-y-3">
            {workflow.map((step, index) => (
              <div
                key={step.title}
                className="flex gap-4 rounded-2xl border bg-fd-background/70 p-4"
              >
                <span className="grid size-8 flex-none place-items-center rounded-full bg-fd-primary text-xs font-bold text-fd-primary-foreground">
                  {index + 1}
                </span>
                <div>
                  <h3 className="text-sm font-semibold text-fd-foreground">{step.title}</h3>
                  <p className="mt-1 text-sm leading-6 text-fd-muted-foreground">
                    {step.description}
                  </p>
                </div>
              </div>
            ))}
          </div>

          <div className="mt-5 rounded-2xl bg-[var(--votrix-accent-soft)] px-4 py-3.5 text-sm leading-6 text-fd-foreground">
            Come back later and continue the same work without starting from scratch.
          </div>
        </div>
      </section>

      <section className="relative border-t bg-fd-muted/30">
        <div className="mx-auto w-full max-w-[1240px] px-6 py-20 lg:px-10 lg:py-28">
          <div className="mb-10 max-w-2xl">
            <div className="mb-3 inline-flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.13em] text-fd-primary">
              <Bot className="size-4" /> What VMA gives you
            </div>
            <h2 className="text-3xl font-semibold tracking-[-0.04em] text-fd-foreground sm:text-4xl">
              Everything your agents need to keep going.
            </h2>
            <p className="mt-4 text-base leading-7 text-fd-muted-foreground">
              You define the work. VMA keeps the agent, its workspace, and its context connected
              from one interaction to the next.
            </p>
          </div>
          <div className="grid gap-4 md:grid-cols-3">
            {capabilities.map(({ icon: Icon, eyebrow, title, description, href }) => (
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
        </div>
      </section>
    </main>
  );
}
