import type {ReactNode} from 'react';
import clsx from 'clsx';
import Link from '@docusaurus/Link';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';

import styles from './index.module.css';

const paths = [
  {
    eyebrow: 'Evaluate',
    title: 'Understand compatibility',
    description:
      'See exactly which Claude Managed Agents resources and behaviors are implemented today.',
    to: '/docs/compatibility-matrix',
  },
  {
    eyebrow: 'Build',
    title: 'Learn the architecture',
    description:
      'Follow the control-plane, runtime, persistence, provider, and sandbox boundaries.',
    to: '/docs/votrix-core-architecture',
  },
  {
    eyebrow: 'Operate',
    title: 'Deploy with confidence',
    description:
      'Review the supported Cloud Run topology and its current scaling constraints.',
    to: '/docs/deployment-platforms',
  },
] as const;

function PathCard({path}: {path: (typeof paths)[number]}): ReactNode {
  return (
    <Link className={styles.pathCard} to={path.to}>
      <span className={styles.cardEyebrow}>{path.eyebrow}</span>
      <Heading as="h3">{path.title}</Heading>
      <p>{path.description}</p>
      <span className={styles.cardLink}>Read more →</span>
    </Link>
  );
}

export default function Home(): ReactNode {
  return (
    <Layout
      title="Open-source infrastructure for long-running agents"
      description="A self-hosted, multi-tenant control plane for long-running agents, built with FastAPI, Deep Agents, and LangGraph.">
      <main>
        <header className={styles.hero}>
          <div className={clsx('container', styles.heroGrid)}>
            <div>
              <div className={styles.eyebrow}>
                Open source · Self hosted · Multi tenant
              </div>
              <Heading as="h1" className={styles.heroTitle}>
                Run long-lived agents on infrastructure you control.
              </Heading>
              <p className={styles.heroLead}>
                Votrix Managed Agents provides a durable control plane aligned
                with the public Claude Managed Agents resource and SDK shape,
                powered by Deep Agents and LangGraph.
              </p>
              <div className={styles.heroActions}>
                <Link
                  className={clsx(
                    'button button--primary button--lg',
                    styles.primaryAction,
                  )}
                  to="/docs/">
                  Read the documentation
                </Link>
                <a
                  className={clsx(
                    'button button--secondary button--lg',
                    styles.secondaryAction,
                  )}
                  href="https://managed-agents.votrix.ai/docs">
                  Explore the API
                </a>
              </div>
              <div className={styles.stack}>
                <span>FastAPI</span>
                <span>Deep Agents</span>
                <span>LangGraph</span>
                <span>Postgres</span>
              </div>
            </div>

            <div className={styles.console} aria-label="Local quick start">
              <div className={styles.consoleBar}>
                <span />
                <span />
                <span />
                <strong>local setup</strong>
              </div>
              <div className={styles.consoleBody}>
                <div>
                  <span className={styles.prompt}>$</span>
                  <code>cp .env.example .env</code>
                </div>
                <div>
                  <span className={styles.prompt}>$</span>
                  <code>uv sync</code>
                </div>
                <div>
                  <span className={styles.prompt}>$</span>
                  <code>uv run alembic upgrade head</code>
                </div>
                <div>
                  <span className={styles.prompt}>$</span>
                  <code>./run.sh</code>
                </div>
                <div className={styles.consoleSuccess}>
                  <span>✓</span>
                  <code>API ready on :8080</code>
                </div>
              </div>
            </div>
          </div>
        </header>

        <section className={styles.statusSection}>
          <div className={clsx('container', styles.statusCard)}>
            <span className={styles.statusLabel}>Project status</span>
            <p>
              VMA is an early 0.1.0 project. Its REST surface is broader than
              its production execution surface, and it is not yet a drop-in
              behavioral replacement.
            </p>
            <Link to="/docs/known-incompatibilities">Review the gaps →</Link>
          </div>
        </section>

        <section className={styles.pathsSection}>
          <div className="container">
            <div className={styles.sectionHeading}>
              <span>Choose your path</span>
              <Heading as="h2">Start with the layer you own.</Heading>
            </div>
            <div className={styles.pathGrid}>
              {paths.map((path) => (
                <PathCard key={path.title} path={path} />
              ))}
            </div>
          </div>
        </section>
      </main>
    </Layout>
  );
}
