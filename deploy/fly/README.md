# Fly.io

Use `deploy/fly/fly.toml` as the Fly app config.

```bash
fly launch --copy-config --config deploy/fly/fly.toml
fly secrets set DATABASE_URL=... ANTHROPIC_API_KEY=... VMA_API_KEY=...
fly secrets set S3_ENDPOINT_URL=... S3_ACCESS_KEY_ID=... S3_SECRET_ACCESS_KEY=...
fly deploy --config deploy/fly/fly.toml
```

The release command runs `scripts/migrate.sh`. The `web` process serves HTTP;
the `worker` process can be scaled separately when queued work needs an
external consumer.
