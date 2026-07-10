# Railway

Railway can build this repository directly from the root `Dockerfile`.

Use `deploy/railway/railway.toml` for the web service:

```bash
railway up --config deploy/railway/railway.toml
```

Create a second Railway service for the worker and use
`deploy/railway/railway.worker.toml`, or set its start command to:

```bash
scripts/start-worker.sh
```

Run migrations once during release:

```bash
railway run scripts/migrate.sh
```

Required variables are the same as `.env.example`: `DATABASE_URL`, provider API
keys, optional `VMA_API_KEY`, and `S3_*`.
