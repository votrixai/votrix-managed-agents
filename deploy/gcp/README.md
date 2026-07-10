# Google Cloud Run

This target uses the common Docker image plus Cloud Run service YAML.

Build and push to Artifact Registry:

```bash
gcloud builds submit \
  --config deploy/gcp/cloudbuild.yaml \
  --substitutions _IMAGE=REGION-docker.pkg.dev/PROJECT/REPOSITORY/votrix-managed-agents:TAG
```

Run migrations once before shifting traffic:

```bash
gcloud run jobs create votrix-managed-agents-migrate \
  --image REGION-docker.pkg.dev/PROJECT/REPOSITORY/votrix-managed-agents:TAG \
  --region REGION \
  --command scripts/migrate.sh
gcloud run jobs execute votrix-managed-agents-migrate --region REGION --wait
```

Deploy the web service after replacing `IMAGE_PLACEHOLDER`:

```bash
gcloud run services replace deploy/gcp/service.staging.yaml --region REGION
```

Deploy workers as a separate Cloud Run service or job using `scripts/start-worker.sh`
when queued self-hosted work needs an external consumer.
