# Private Organization Onboarding SOP

Internal only. The filename is retained for existing runbook links; this is no
longer a superadmin HTTP workflow.

## Supported flow

A signed-in Developer App user with no active VMA membership may create exactly
their initial Organization from the empty console state. The browser posts to
the Developer App's same-origin `/api/organizations` BFF. The BFF:

1. rejects cross-origin requests and requires the explicit onboarding action
   header;
2. verifies the Supabase session and forwards its short-lived access token;
3. obtains a Vercel OIDC token server-side;
4. exchanges it through Google Workload Identity Federation and impersonates
   the dedicated control-plane invoker service account; and
5. calls `POST /internal/organizations` on the private control-plane Cloud Run
   service with both service and human authentication.

There is no public VMA Organization-creation route, Cloudflare path, SDK method,
static Google service-account key, or browser-visible Cloud Run URL. Operators
must not bypass this boundary by adding `allUsers` as a Cloud Run invoker.

## Provisioning semantics

The operation is durable and resumable for one Supabase user ID:

- one onboarding-request row is persisted before Organization or provider
  effects;
- an expiring database lease permits only one provisioning worker for that
  request across all control-plane instances;
- retries with the same normalized name resume the same Organization;
- retries with a different name return a conflict;
- the service creates the default Account and OpenRouter inference key before
  granting the owner membership;
- membership becomes visible only after the Account is active;
- a user who already has an active membership cannot self-provision another
  Organization; and
- removing that membership later does not allow the onboarding request to be
  replayed to regain access.

The request does not create or reveal an Organization API key. API keys remain
a separate authenticated lifecycle for approved machine integrations.

## Deployment and IAM checks

Run `scripts/gcloud/9-setup-vercel-control-plane.sh` once, then deploy through
the normal staging or production script. Verify all of the following without
creating a live Organization:

1. the public API returns `404` for `POST /v1/organizations`;
2. an unauthenticated request to the private control-plane URL is rejected by
   Cloud Run IAM;
3. the private service has no `allUsers` invoker binding;
4. only `vma-developer-app-invoker@votrixai-480422.iam.gserviceaccount.com`
   has `roles/run.invoker` on that service; and
5. the Workload Identity provider accepts only the immutable Vercel team and
   project IDs, while service-account bindings name the exact production and
   staging subjects.

## Recovery

If provisioning stops after the onboarding row or Organization is created,
the user should submit the same Organization name again. Do not delete rows or
mint a second provider key manually: the service resumes the stored request and
reuses the Organization and Account rows it already recorded. A live lease can
briefly return an in-progress conflict; a dead worker's lease expires after five
minutes.

If OpenRouter minted a key but the control plane stopped before storing its
encrypted credential, VMA deliberately refuses to mint another. An operator
must identify and revoke the orphaned provider key using the Account's expected
provider-key name, then let the user retry.

If retry still fails, inspect the onboarding request, Organization, default
Account, and OpenRouter provider-key state as one unit. Keep owner membership
absent until the default Account is active. Any manual database repair requires
a reviewed incident procedure and an audit note; never expose the private route
or a database credential to the user.

For local/operator-created test tenants, use `scripts/bootstrap_api_key.py` in
a trusted environment. That path is not self-service onboarding and must not be
used by end users.
