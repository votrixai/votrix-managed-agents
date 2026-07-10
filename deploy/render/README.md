# Render

Use `deploy/render/render.yaml` as a Render Blueprint.

The web service and worker share the same Dockerfile. Render runs
`scripts/migrate.sh` as the web service pre-deploy command, then starts:

- Web: `scripts/start-web.sh`
- Worker: `scripts/start-worker.sh`

Set the `sync: false` secrets in the Render dashboard before deploying.
