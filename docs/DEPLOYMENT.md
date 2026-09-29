# Deployment runbook

This runbook takes the DCF app from a laptop to production at the lowest cost that works for v1, about 20–50 users. The backend runs on **one Graviton EC2 instance with docker compose**, provisioned by Terraform. The **frontend is on Vercel**. **Postgres and Auth are on Supabase.** Artifacts are in **S3**.

The runbook has two parts:
- **[Part A: Manual steps (account owner)](#part-a-manual-steps-account-owner)** covers accounts, consoles, API keys, DNS and the GitHub/Vercel settings.
- **[Part B: Scripted / automated](#part-b-scripted--automated)** covers everything that is one command, or that runs by itself on every push to `main`.

Use the [order of operations](#order-of-operations) table to move between the two parts.

## Architecture

```
 browser ──HTTPS──> Vercel (Next.js frontend; NEXT_PUBLIC_* only, no server secrets)
    │
    └──HTTPS (CORS)──> EC2 t4g.small, Elastic IP, Amazon Linux 2023 arm64   (security group: 80/443 only)
                        └─ docker compose  (/opt/dcf/docker-compose.prod.yml)
                             caddy :80/:443  auto HTTPS (Let's Encrypt) for <api host>, SSE flushed immediately
                               └─> api :8000     uvicorn, 2 processes
                             worker            SAQ + LibreOffice + WeasyPrint
                             redis :6379       queue / locks / rate limiter / SSE fan-out, no persistence
                        api + worker ──> Supabase Postgres (session pooler), S3 (instance role),
                                          SEC EDGAR, Yahoo, FRED, Anthropic

 GitHub Actions (push to main) ──OIDC──> ECR push (arm64 images) ──> SSM Run Command ──> /opt/dcf/deploy.sh <sha>
```

| Decision | Choice | Why / alternative |
|---|---|---|
| Compute | **One EC2 `t4g.small`** (2 vCPU burstable, 2 GB RAM, plus a 2 GB swapfile), arm64 | About $12/month. The whole backend (api, worker, redis, caddy) fits. `t4g.medium` (4 GB) is the upgrade path, [below](#upgrade-the-instance). There is no high availability: a host failure is auto-recovered by EC2, and a deploy restarts the containers, which takes a few seconds. |
| Frontend | **Vercel** (Hobby tier) | Free for personal, non-commercial use, and it gives HTTPS, previews and git-push deploys. The browser calls the API cross-origin, so the API allows the Vercel origin(s) through `CORS_ORIGINS`. |
| HTTPS for the API | **Caddy** with automatic Let's Encrypt | Caddy needs no load balancer and no certificate management. Without a domain, the hostname is `<eip-with-dashes>.sslip.io`, a public wildcard DNS name that resolves to the Elastic IP. A custom domain is [optional](#a7-custom-domain-optional). |
| Postgres + Auth | **Supabase** (session pooler) | Nothing stateful runs on the box. The direct DB host is IPv6-only and the default VPC is IPv4-only, so migrations also use the session pooler. |
| Artifacts | **S3**, accessed with the **instance role** | There are no static keys. `models/` and `runs/` expire after 35 days. |
| Images | **ECR** (`dcf-api`, `dcf-worker`), tagged with the git SHA, last 10 kept | The instance pulls with its role. |
| Secrets | **SSM Parameter Store** SecureStrings under `/dcf/prod/*`, written by `put_ssm_params.sh` | Secrets never enter git, images or Terraform state. Each deploy renders `/opt/dcf/.env` (0600) from SSM. |
| Shell access | **SSM Session Manager** | No SSH port is open and there is no key pair. |
| Deploys | **GitHub Actions → OIDC → ECR → SSM Run Command** | No inbound access is needed for deploys. The same flow runs from a laptop with `infra/scripts/deploy.sh`. |
| IaC | **Terraform** (`infra/terraform/`), S3 state with native lockfiles | No DynamoDB table is needed (Terraform ≥ 1.10). |

What lives where:

| Path | What |
|---|---|
| `infra/terraform/bootstrap/` | The Terraform state bucket (one time) |
| `infra/terraform/` | VPC lookups, security group, EIP, instance and user data, IAM (instance role, GitHub OIDC deploy role), S3, ECR, SSM config parameters, budget, status-check alarms |
| `infra/deploy/docker-compose.prod.yml`, `infra/deploy/Caddyfile` | The production stack. It is shipped to the box with every deploy |
| `infra/scripts/deploy_remote.sh` | Runs **on the box** as `/opt/dcf/deploy.sh <sha>` |
| `infra/scripts/deploy.sh` | Builds and pushes images (optional), uploads the bundle, triggers the box through SSM. Used by CI and for manual deploys |
| `infra/scripts/put_ssm_params.sh` | Copies `.env` values into SSM |
| `infra/scripts/seed_damodaran_cache.py` | Seeds the Damodaran datasets into S3 |
| `infra/docker/Dockerfile.{api,worker}` | Production images, linux/arm64 |
| `infra/docker/Dockerfile.frontend` | Only for the local `docker-compose.yml`. Production uses Vercel |

## Order of operations

| # | Step | Who | Section |
|---|---|---|---|
| 1 | AWS account, budget awareness, local tools | **Owner** | [A1](#a1-prerequisites) |
| 2 | Supabase project, keys, connection strings, auth | **Owner** | [A2](#a2-supabase) |
| 3 | Anthropic key and spend limit, FRED key, SEC User-Agent | **Owner** | [A3](#a3-anthropic-fred-sec-edgar) |
| 4 | Fill in `terraform.tfvars`, then Terraform: state bucket, then `apply` | **Owner** + **Scripted** (the owner runs it) | [A4](#a4-choose-the-terraform-variables), [B1](#b1-terraform) |
| 5 | Secrets into SSM | **Scripted** | [B2](#b2-secrets-into-ssm) |
| 6 | GitHub secret and variables, `DEPLOY_ENABLED=true` | **Owner** | [A6](#a6-github-actions) |
| 7 | First deploy (push to `main`, or `deploy.sh`) | **Automated** / **Scripted** | [B3](#b3-deploy) |
| 8 | Seed the Damodaran cache | **Scripted** | [B4](#b4-seed-the-damodaran-datasets) |
| 9 | Vercel project and env vars, then `cors_origins` → `apply` → redeploy API | **Owner** + **Scripted** | [A5](#a5-vercel-frontend) |
| 10 | Supabase Site URL and redirect URLs set to the Vercel URL | **Owner** | [A2](#a2-supabase) step 7 |
| 11 | Optional: custom API domain | **Owner** + **Scripted** | [A7](#a7-custom-domain-optional) |
| 12 | Smoke test | **Owner** | [A8](#a8-smoke-test) |
| — | After that, every push to `main` redeploys the API (Actions) and the frontend (Vercel) | automatic | |

---

# Part A: Manual steps (account owner)

## A1. Prerequisites

1. **AWS account.** Sign in with IAM Identity Center or an admin profile (`aws configure sso`). Never use root keys. The resources here cost about **$19/month** ([cost table](#monthly-cost)). New accounts on the credit-based Free Plan draw the credit down. EC2, EBS and public IPv4 are not Always-Free.
2. **Budget.** Terraform creates a monthly budget of **$30** (`monthly_budget_usd`), which alerts `alert_email` at 80% and 100% of actual spend and at 100% of forecast. Optionally, turn on Cost Anomaly Detection in the console. It is free.
3. **Local tools** on the machine that runs Terraform and the scripts:
   - Terraform **≥ 1.10**
   - AWS CLI v2
   - the [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) for `aws ssm start-session`
   - git and python3
   - for manual image builds only: Docker with buildx. Apple Silicon builds arm64 natively. On x86 you need QEMU/binfmt, which Docker Desktop includes. On Linux, run `docker run --privileged --rm tonistiigi/binfmt --install arm64`.
4. **GitHub**: admin on the repository, for secrets and variables.
5. **Vercel** account, which can be created with GitHub login.

## A2. Supabase

1. Create a project at supabase.com. The free tier is enough. Pick the **same region as AWS**, `us-east-1` by default, and save the DB password. Free projects **pause after about a week without activity**, so un-pause the project in the dashboard if that happens.
2. Under **Project Settings → Data API**, copy the Project URL into `.env` as `SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_URL`.
3. Under **Project Settings → API Keys**:
   - Put the **publishable/anon** key in `SUPABASE_ANON_KEY` and `NEXT_PUBLIC_SUPABASE_ANON_KEY`. It is public by design.
   - Put the **secret/service_role** key in `SUPABASE_SERVICE_ROLE_KEY`. It is for the backend only. Never put it in Vercel.
4. Under **Project Settings → JWT Keys**, make sure the current signing key is **asymmetric** (ECC P-256 or RS256), not the legacy HS256 secret. If it is not, click **Migrate JWT secret**, then **rotate**. Do this **before** real users sign in, because rotating invalidates sessions. There is nothing to copy: the API reads `{SUPABASE_URL}/auth/v1/.well-known/jwks.json`.
5. Click **Connect** and copy the **Session pooler** string (`…pooler.supabase.com:5432`, user `postgres.<ref>`). Change the `postgresql://` prefix to **`postgresql+psycopg://`**, then put it in **both** `DATABASE_POOLER_URL` and `DATABASE_URL`.
   - Do not use the transaction pooler on port 6543.
   - The direct host `db.<ref>.supabase.co` is **IPv6-only** unless you buy the IPv4 add-on. The EC2 default VPC is IPv4-only. The session pooler handles DDL, so production runs `alembic upgrade head` through it. `put_ssm_params.sh` stores the pooler value as `DATABASE_URL` by default.
6. Under **Authentication → Sign In / Providers**, enable Email (magic link). Either invite users under **Authentication → Users → Invite**, or leave sign-ups open.
7. Under **Authentication → URL Configuration**, set **Site URL** to the Vercel production URL once it exists ([A5](#a5-vercel-frontend)). Add the following to **Redirect URLs**:
   - `https://<your-app>.vercel.app/**`
   - `http://localhost:3000/**` for local dev
   - optionally `https://*-<team>.vercel.app/**`, so sign-in works on preview deployments
8. Sanity check after the first deploy, which runs the migrations. In the SQL editor, `select policyname, tablename from pg_policies where schemaname = 'public';` should list the RLS policies from SPEC §3.

## A3. Anthropic, FRED, SEC EDGAR

- **Anthropic.** At console.anthropic.com, go to **API Keys → Create key** and copy it into `ANTHROPIC_API_KEY`. Confirm that `ANTHROPIC_MODEL` (default `claude-sonnet-5`) is enabled for the org. **Set a monthly spend limit** under **Limits**.
- **FRED.** Get a free key at fred.stlouisfed.org/docs/api/api_key.html and put it in `FRED_API_KEY`.
- **SEC EDGAR.** There is no key. Set `SEC_EDGAR_USER_AGENT` to a real app name and contact email, for example `DCF-Valuation-App aman.todi01@gmail.com`. Non-compliant or bursty clients get a 403 or an IP block. The app's Redis token bucket keeps requests under 10 req/s.

Collect everything in the git-ignored `.env`, starting from `cp .env.example .env`. `put_ssm_params.sh` reads it.

## A4. Choose the Terraform variables

Run `cp infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars` (the copy is git-ignored), then set:

| Variable | Value |
|---|---|
| `aws_region` | `us-east-1`, or the region of your Supabase project |
| `github_owner` / `github_repo` | for example `aman-todi` / `intrinsic-valuation`. Only `refs/heads/main` of this repo can assume the deploy role |
| `alert_email` | budget alerts and the Let's Encrypt contact |
| `cors_origins` | `[]` for now. After [A5](#a5-vercel-frontend), `["https://<your-app>.vercel.app"]` |
| `domain_name` | optional; empty = `<eip>.sslip.io` |
| `instance_type` / `worker_concurrency` | `t4g.small` / `2` (the defaults) or `t4g.medium` / `4` |
| `monthly_budget_usd` | `30` (the default) |

Then run Part B, [B1–B2](#b1-terraform).

## A5. Vercel (frontend)

You need `terraform output api_url` first, which looks like `https://3-91-20-7.sslip.io`.

1. At vercel.com, go to **Add New → Project** and import the GitHub repo.
2. Set **Root Directory** to `frontend`. Next.js is auto-detected. The `output: "standalone"` setting in `next.config.ts` is harmless on Vercel.
3. Add these environment variables for **Production**, and for **Preview** if you use previews:
   - `NEXT_PUBLIC_API_BASE_URL` = the `api_url` output, **https**, with no trailing slash
   - `NEXT_PUBLIC_SUPABASE_URL` = `https://<ref>.supabase.co`
   - `NEXT_PUBLIC_SUPABASE_ANON_KEY` = the publishable/anon key. **Never** add the service role key.
4. Deploy, then note the production URL, for example `https://dcf-valuation.vercel.app`.
5. Allow that origin on the API:
   1. In `terraform.tfvars`, set `cors_origins = ["https://dcf-valuation.vercel.app"]`. Use the exact origin, with no trailing slash.
   2. Run `terraform apply`.
   3. **Redeploy the API**: re-run the latest `deploy` workflow, or run `infra/scripts/deploy.sh --skip-build --tag <current sha>`. The deploy re-renders `.env` and recreates the containers.
6. Put the same URL in Supabase **Site URL / Redirect URLs** ([A2](#a2-supabase) step 7).
7. From now on, Vercel builds and deploys the frontend on every push to `main`, through its own Git integration. `NEXT_PUBLIC_*` values are inlined at build time, so after changing one in Vercel, redeploy the frontend.

**Preview deployments** get per-branch or per-commit URLs that are not in `CORS_ORIGINS`, so their API calls fail CORS. `CORS_ORIGINS` is an explicit list, and wildcards or regexes are not supported by the current settings. You have two options:
- Test on production.
- Add a **stable** preview alias, such as a branch domain like `dcf-git-staging-<team>.vercel.app`, to `cors_origins`.

The trade-off: every listed origin can make credentialed browser calls to the API. Each call still needs a valid Supabase JWT, because CORS is not authentication, but keep the list short.

**Vercel Hobby is free but limited to personal, non-commercial use.** Commercial use needs Pro, at $20 per user per month.

## A6. GitHub Actions

Go to **Settings → Secrets and variables → Actions**.

| Kind | Name | Value |
|---|---|---|
| Secret | `AWS_DEPLOY_ROLE_ARN` | `terraform output -raw github_deploy_role_arn` |
| Variable | `DEPLOY_ENABLED` | `true`. While unset, `deploy.yml` only posts a notice, so `main` stays green |
| Variable | `AWS_REGION` | optional, default `us-east-1`. Must match `aws_region` |
| Variable | `ARM_RUNNER` | optional, default `ubuntu-24.04-arm` (native arm64 GitHub-hosted runner). If arm64 hosted runners are not available for this repository or plan, so the job never starts, set it to `ubuntu-latest`. The images are then built under QEMU emulation, which is slower |

The workflow needs nothing else. It reads the instance id, bucket and registry from SSM `/dcf/prod/config/*`, which Terraform writes. The frontend's `NEXT_PUBLIC_*` values live in Vercel, not in GitHub.

Protect `main` with required status checks (`ci-backend`, `ci-frontend`). The deploy workflow does not wait for CI.

## A7. Custom domain (optional)

The sslip.io hostname works with no purchase. It depends on a free third-party DNS service and on Let's Encrypt limits shared with everyone else who uses sslip.io, although Caddy retries and falls back to ZeroSSL. For anything beyond a pilot, use a cheap domain (about $10/year):

1. At your DNS provider, create **`A api.example.com → <elastic_ip>`**, using `terraform output elastic_ip`. Wait until `dig +short api.example.com` returns the IP.
2. In `terraform.tfvars`, set `domain_name = "api.example.com"` and run `terraform apply`. This updates the SSM config (`APP_DOMAIN`, `PUBLIC_API_BASE_URL`) and does not touch the instance.
3. Redeploy the API. Caddy is recreated with the new hostname and gets a certificate within seconds. Check with `curl https://api.example.com/api/health`.
4. In Vercel, set `NEXT_PUBLIC_API_BASE_URL=https://api.example.com` and redeploy the frontend.

A custom domain for the **frontend** is added in Vercel (**Project → Domains**). Add it to `cors_origins` (then apply and redeploy) and to the Supabase URLs.

## A8. Smoke test

1. Run `curl https://<api host>/api/health`, which should return `{"status":"ok"}`. HTTP must redirect to HTTPS.
2. Run `curl -si -X OPTIONS https://<api host>/api/runs -H 'Origin: https://<your-app>.vercel.app' -H 'Access-Control-Request-Method: POST' | grep -i access-control-allow-origin`. It should echo the Vercel origin.
3. Sign in on the Vercel URL with a magic link. The link must come back to the Vercel URL, not to localhost.
4. Run a plain FCFF name such as **`AAPL`**: confirm the proposed assumptions, then build.
5. The progress screen should update **live** as steps complete, which is SSE through Caddy. If all updates arrive only at the end, something is buffering. Check the `flush_interval -1` line in `infra/deploy/Caddyfile`.
6. Download the **`.xlsx`** and the **`.pdf`**. The links are presigned S3 URLs that expire. Check that the workbook recalculates in Excel.
7. Start another run and **cancel** it mid-build. It should move to `cancelled`.
8. Optionally, redeploy while a build runs. The worker drains for up to 60 s (`WORKER_SHUTDOWN_GRACE_SECONDS`) and then cancels cleanly, and the run shows "worker restarted".

---

# Part B: Scripted / automated

## B1. Terraform

All commands run in `infra/terraform/`, with admin credentials in your shell (`aws sts get-caller-identity` to confirm).

```bash
# 4a. State bucket (once per account). It uses local state, which is git-ignored, and the bucket has prevent_destroy.
cd infra/terraform/bootstrap
terraform init && terraform apply          # creates dcf-tfstate-<account-id>, versioned + encrypted
terraform output -raw backend_hcl > ../backend.hcl
cd ..

# 4b. Main stack
terraform init -backend-config=backend.hcl # S3 backend, use_lockfile = true (no DynamoDB)
terraform plan -out tfplan                 # needs terraform.tfvars (A4)
terraform apply tfplan
terraform output                           # api_url, elastic_ip, instance_id, github_deploy_role_arn, ...
```

Commit the generated `infra/terraform/.terraform.lock.hcl` so every machine uses the same provider build. If the account already has a GitHub OIDC provider, set `github_oidc_provider_arn`, because an account can only have one.

What it creates (prefix `dcf`):

| Resource | Details |
|---|---|
| `aws_instance.app` | AL2023 **arm64**, `t4g.small`, a default-VPC subnet in an AZ that offers the type, IMDSv2 only (hop limit 2 so containers can use the role), gp3 30 GB **encrypted** root, auto-recovery on, AMI and subnet changes ignored (no surprise replacement), tags `Name`/`Role=dcf-app` |
| `user_data` (`templates/user_data.sh.tftpl`) | Runs once: Docker, compose plugin (pinned and checksum-verified), 2 GB swapfile, json-file log rotation (10 MB × 5), `live-restore`, `/opt/dcf`, a `dcf-compose` shell alias. App files arrive with each deploy |
| `aws_eip.app` + association | A stable public IPv4, which the sslip.io name and your DNS point at |
| `aws_security_group.app` | Inbound TCP 80/443 and UDP 443 (HTTP/3) from `0.0.0.0/0` and `::/0`. **No SSH.** All egress |
| IAM `dcf-app-instance` role and profile | `AmazonSSMManagedInstanceCore`; ECR pull (2 repos); S3 List/Get/Put/Delete on the bucket; `ssm:GetParameter*` on `/dcf/prod/*`; `kms:Decrypt` only via SSM |
| GitHub OIDC provider and `dcf-github-deploy` role | Trust: `repo:<owner>/<repo>:ref:refs/heads/main`. Permissions: ECR push to the 2 repos, `s3:PutObject` on `deploy/*`, read `/dcf/prod/config/*` (not the secrets), `ssm:SendCommand` only on this instance plus `AWS-RunShellScript`, and the read-only `GetCommandInvocation`/`DescribeInstanceInformation` |
| S3 `dcf-app-artifacts-<account-id>` | Public access blocked, SSE-S3, TLS-only policy, BucketOwnerEnforced. Lifecycle: `models/` and `runs/` expire after **35 days**, `deploy/` bundles after 90 days, abort incomplete multipart uploads after 7 days. `edgar-raw/` and `damodaran/` are kept |
| ECR `dcf-api`, `dcf-worker` | Scan on push; lifecycle **keeps the last 10** images |
| SSM `/dcf/prod/config/*` (String) | `APP_DOMAIN`, `PUBLIC_API_BASE_URL`, `CORS_ORIGINS`, `ACME_EMAIL`, `AWS_REGION`, `S3_BUCKET_NAME`, `ECR_REGISTRY`, `INSTANCE_ID`, `WORKER_CONCURRENCY`. These are non-secret values derived from the variables |
| `aws_budgets_budget.monthly` | $30/month, email at 80% and 100% of actual spend and 100% of forecast |
| CloudWatch alarms (`enable_status_check_alarms`) | System status check failure → **recover**; instance status check failure (for example an OOM hang) → **reboot** |

## B2. Secrets into SSM

```bash
infra/scripts/put_ssm_params.sh --dry-run    # validates .env, writes nothing
infra/scripts/put_ssm_params.sh              # uses the same region as Terraform (AWS_REGION or aws configure)
```

It writes these parameters under `/dcf/prod/`:

| Parameter | Type | From `.env` |
|---|---|---|
| `SUPABASE_ANON_KEY` | SecureString | |
| `SUPABASE_SERVICE_ROLE_KEY` | SecureString | |
| `DATABASE_URL` | SecureString | **the `DATABASE_POOLER_URL` value** by default (IPv4-only host). Pass `--direct-migrations` to keep the direct URL if you bought Supabase's IPv4 add-on |
| `DATABASE_POOLER_URL` | SecureString | |
| `ANTHROPIC_API_KEY` | SecureString | |
| `FRED_API_KEY` | SecureString | |
| `SUPABASE_URL` | String | |
| `SEC_EDGAR_USER_AGENT` | String | |
| `ANTHROPIC_MODEL` | String | default `claude-sonnet-5` |

Safeguards:
- It never prints values, and it passes them through 0600 temp files rather than argv.
- It refuses `.env.example` placeholders, DB URLs without `+psycopg`, and transaction-pooler URLs.
- `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` are **not** copied, because the instance role replaces them.

The deploy **pins** these values in `docker-compose.prod.yml`, and they cannot be overridden from SSM: `STORAGE_BACKEND=s3`, `DATA_SOURCE_MODE=live`, `DEV_AUTH_BYPASS=false`, `REDIS_URL=redis://redis:6379`, `WORKER_SHUTDOWN_GRACE_SECONDS=60`.

## B3. Deploy

### Automatic: every push to `main`

`.github/workflows/deploy.yml` runs when `DEPLOY_ENABLED == 'true'`:
1. **build** (matrix `api`, `worker`, on native arm64 runners): OIDC → ECR login → `docker buildx` for `linux/arm64` with a GitHub Actions layer cache → push `dcf-<image>:<git sha>`.
2. **deploy**: OIDC → `infra/scripts/deploy.sh --skip-build --tag <sha>`.

A single concurrency group means deploys never overlap. You can also run it by hand with **Actions → deploy → Run workflow** on `main`.

### Manual, from a laptop

```bash
infra/scripts/deploy.sh                   # build+push arm64 images for HEAD (skipped if the tag is already in ECR), then deploy
infra/scripts/deploy.sh --skip-build --tag <sha>   # redeploy an existing image, e.g. after changing SSM/Terraform config
infra/scripts/deploy.sh --skip-migrations --skip-build --tag <older-sha>   # rollback
```

The script refuses a dirty working tree unless `ALLOW_DIRTY=true`.

### What a deploy does

`deploy.sh` runs these steps:
1. It reads `/dcf/prod/config/{INSTANCE_ID,S3_BUCKET_NAME,ECR_REGISTRY}`.
2. It builds and pushes the images, unless `--skip-build`.
3. It tars `docker-compose.prod.yml`, `Caddyfile` and `deploy_remote.sh` and uploads them to `s3://<bucket>/deploy/<sha>.tar.gz`.
4. It waits for the SSM agent to be online.
5. It sends **`AWS-RunShellScript`** to the instance. That script waits for cloud-init (first boot), downloads the bundle and runs `deploy_remote.sh <sha>`. `deploy.sh` then polls until the command finishes and prints its output.

On the box, `deploy_remote.sh` does the following:
1. It installs the shipped files to `/opt/dcf/`: `docker-compose.prod.yml`, `caddy/Caddyfile`, and `deploy.sh`, which is itself.
2. It **renders `/opt/dcf/.env`** (0600) from every parameter under `/dcf/prod`. The Terraform `config/*` values win on a name clash. It adds `IMAGE_TAG=<sha>`, and it refuses to continue if a required key is missing.
3. It logs in to ECR, then runs `docker compose pull`.
4. It runs **`alembic upgrade head`** in a one-off container of the **new** api image (`docker compose run --rm --no-deps api …`). **If that fails, it aborts**: the previous `.env` is restored and the running containers are not touched.
5. It runs `docker compose up -d --remove-orphans`, which recreates the containers whose image or env changed. It reloads Caddy if the Caddyfile changed.
6. It waits for the api healthcheck and for all 4 services to be running. It prints logs and fails if they are not ready after 180 s.
7. It probes `https://<api host>/api/health` through Caddy. This is only a warning on the first deploy, while the certificate is being issued.
8. It records `CURRENT_TAG` and `PREVIOUS_TAG` and runs `docker image prune -af`.

Deploys cause a short blip, a few seconds while the api container restarts. Open SSE streams drop, and `EventSource` reconnects on its own. The worker gets SIGTERM with a 90 s `stop_grace_period`: SAQ drains for 60 s (`WORKER_SHUTDOWN_GRACE_SECONDS`), then cancels the remaining jobs cleanly. **Keep migrations backward-compatible** (expand, then contract), because they run while the old containers are still serving.

The first deploy after `terraform apply` can take 5–10 minutes: cloud-init, the first image pull (about 1.5 GB for the worker with LibreOffice), and certificate issuance.

## B4. Seed the Damodaran datasets

This runs from a laptop with your AWS credentials and the backend venv:

```bash
cd backend
.venv/bin/python ../infra/scripts/seed_damodaran_cache.py --dry-run
.venv/bin/python ../infra/scripts/seed_damodaran_cache.py --bucket "$(terraform -chdir=../infra/terraform output -raw s3_bucket)"
```

If pages.stern.nyu.edu blocks the script, download `betas.xls`, `margin.xls` and `histimpl.xls` in a browser and add `--from-local-dir ~/Downloads/damodaran`. Re-run it in January, when Damodaran republishes. There is no automatic refresh. Until the cache is seeded, the app uses the bundled snapshot.

---

# Operations

```bash
# Shell (no SSH): needs the Session Manager plugin
aws ssm start-session --target "$(terraform -chdir=infra/terraform output -raw instance_id)"
sudo -i && cd /opt/dcf                 # everything lives here; `dcf-compose` = docker compose for this stack

dcf-compose ps                          # status and health
dcf-compose logs -f --tail 200 api worker caddy   # logs (json-file, rotated 10 MB x 5 per container)
dcf-compose restart worker              # restart one service
cat /opt/dcf/CURRENT_TAG /opt/dcf/PREVIOUS_TAG
free -m; docker stats --no-stream      # memory pressure on t4g.small
sudo SKIP_MIGRATIONS=true /opt/dcf/deploy.sh <sha>   # re-run / roll back from the box (image must be in ECR)
```

| Task | How |
|---|---|
| **Rollback** | `infra/scripts/deploy.sh --skip-build --skip-migrations --tag <older-sha>`, or `/opt/dcf/deploy.sh` on the box. ECR keeps the last 10 images. Migrations are never downgraded automatically. |
| **Change config** (CORS, domain, worker concurrency) | Edit `terraform.tfvars`, run `terraform apply`, then redeploy (`deploy.sh --skip-build --tag <current sha>`). |
| **Rotate a secret** | Update `.env`, run `put_ssm_params.sh`, then redeploy. The deploy re-renders `.env`, and compose recreates the api and worker. For Supabase keys and the DB password, rotate in Supabase first. After rotating the anon key, also update Vercel and redeploy the frontend. |
| **Backups** | Nothing on the box needs a backup. Postgres is Supabase. The free tier has no point-in-time recovery, so schedule a `pg_dump` over the session pooler if the data matters. Artifacts are in S3 and are regenerable caches. The only local state is the `caddy_data` volume (certificates), which is re-issued automatically if lost. Avoid destroying it repeatedly, because of Let's Encrypt rate limits. |
| **OS patches** | Monthly, from a session: `sudo dnf upgrade --releasever=latest -y && sudo reboot`. Docker and every container (`restart: unless-stopped`) come back on their own. |
| **Disk** | `docker system df`. Each deploy prunes unused images. The root volume is 30 GB. |
| **Host failure** | EC2 auto-recovery, plus the CloudWatch alarms that recover or reboot the instance. The EIP, volume and instance id stay the same. |
| **Pause to save money** | `aws ec2 stop-instances --instance-ids <id>` stops compute billing. The EIP (about $3.65/month) and EBS (about $2.40/month) still bill. `start-instances` brings everything back. |

### Upgrade the instance

Signs that you need to upgrade: builds get OOM-killed (`dmesg | grep -i oom`), swap stays heavily used, or the instance-status alarm reboots the box.

1. In `terraform.tfvars`, set `instance_type = "t4g.medium"` and `worker_concurrency = 4`, and raise `monthly_budget_usd` to about 40.
2. Run `terraform apply`. It stops and starts the instance in place, with about 2 minutes of downtime. The EIP, disk and ID stay the same.
3. Redeploy, to pick up `WORKER_CONCURRENCY`.

### Teardown

1. Set `force_destroy = true` and run `terraform apply`. This allows deleting a non-empty bucket and ECR repos that hold images.
2. Run `terraform destroy`.
3. Delete the secrets, which are not in Terraform state: `aws ssm delete-parameters --names $(aws ssm get-parameters-by-path --path /dcf/prod --query 'Parameters[].Name' --output text)`.
4. The state bucket has `prevent_destroy`. To remove it, empty all its versions in the console, delete the bucket, and delete `infra/terraform/bootstrap/terraform.tfstate`.
5. Delete the Vercel project and the Supabase project separately.

---

# Monthly cost

This estimate is for on-demand pricing in us-east-1, running 730 hours a month, with low traffic. Check current prices before you commit.

| Item | Size | ≈ $/month |
|---|---|---|
| EC2 `t4g.small` | 2 vCPU burstable, 2 GB, $0.0168/h | 12.26 |
| EBS gp3 root | 30 GB × $0.08 | 2.40 |
| Public IPv4 (Elastic IP, attached) | $0.005/h | 3.65 |
| S3 | a few hundred MB plus requests | ~0.10 |
| ECR | 2 repos × up to 10 images, shared layers (~2 GB) | ~0.20 |
| Data transfer out | first 100 GB/month free | ~0 |
| CloudWatch alarms | 2 standard alarms (10 free) | 0–0.20 |
| SSM Parameter Store (standard), Run Command, Session Manager, Budgets, OIDC | | 0 |
| Terraform state bucket | | ~0.01 |
| **Total AWS** | | **≈ $19** |
| `t4g.medium` instead (4 GB, $0.0336/h) | | ≈ $31 |
| Burst surplus with `cpu_credits = "unlimited"` | only if average CPU stays above the 20%/vCPU baseline | usually 0 |
| Vercel Hobby (personal, non-commercial) · Supabase free · FRED / EDGAR / Damodaran | | 0 |
| Anthropic | per token | usage-based; set a spend limit |

For comparison, the previous ECS Fargate + ALB design cost about $70–100/month.

---

# Local development

### Option A: docker compose

```bash
cp .env.example .env     # real dev values; DATABASE_*_URL can point at Supabase or at a local Postgres
docker compose up --build   # redis :6379, api :8000, worker, frontend :3000 (uses infra/docker/Dockerfile.*)
```

Compose overrides `REDIS_URL` to point at the redis container. The frontend image is built with `NEXT_PUBLIC_API_BASE_URL=http://localhost:8000` and the `NEXT_PUBLIC_SUPABASE_*` values from `.env`. If those are empty, the frontend uses the dev auth bypass.

Inside the containers, `localhost` means the container itself. To use a Postgres running on the host, set the URLs to `host.docker.internal` on macOS or Windows. On Linux, add `extra_hosts: ["host.docker.internal:host-gateway"]`.

To try the production compose file locally, run `docker compose -f infra/deploy/docker-compose.prod.yml config` with a filled-in `.env`. It pulls from ECR and needs a public hostname for the certificate, so it is not meant for laptops.

### Option B: run the processes directly (fastest edit loop)

```bash
# Postgres 16 locally (instead of Supabase) - the initial migration creates stub auth.users/auth.uid()/auth.role()
# when the Supabase `auth` schema is absent, so plain Postgres works.
PG=/usr/lib/postgresql/16/bin            # or: brew install postgresql@16
$PG/initdb -D ~/.dcf-pg -U postgres --auth=trust && $PG/pg_ctl -D ~/.dcf-pg -l ~/.dcf-pg/log start
#   DATABASE_URL=DATABASE_POOLER_URL=postgresql+psycopg://postgres@localhost:5432/postgres  in .env
redis-server --save '' --appendonly no &            # or: docker run -p 6379:6379 redis:7-alpine

cd backend
uv venv -p python3.13 .venv && uv pip install -p .venv -e '.[dev]'
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload --port 8000                  # terminal 1
.venv/bin/saq app.jobs.worker_settings.settings -v                   # terminal 2 (same as Dockerfile.worker CMD)

cd ../frontend && npm ci
npm run dev          # terminal 3 -> http://localhost:3000 (uses NEXT_PUBLIC_* from the environment / frontend/.env.local)
npm run dev:mock     # UI only, API mocked with MSW - no backend needed
```

Notes:
- With a local Postgres, Supabase Auth is not present. You have two options:
  - Keep `SUPABASE_URL` and the anon key pointing at a real Supabase project, which then handles only sign-in and JWKS.
  - Set `DEV_AUTH_BYPASS=true`. The frontend, with `NEXT_PUBLIC_SUPABASE_URL` unset, sends `Bearer dev-bypass-token`, and the API maps it to a fixed dev user that it inserts into the stub `auth.users`. **Never** enable this in production. `docker-compose.prod.yml` pins `DEV_AUTH_BYPASS=false`.
- `STORAGE_BACKEND=local`, with `LOCAL_STORAGE_DIR` and `PUBLIC_API_BASE_URL`, writes artifacts to disk instead of S3. Download links become signed, expiring `GET /api/files/...` URLs on the API. Production pins `s3`.
- Without `ANTHROPIC_API_KEY`, proposals and report prose use the deterministic fallback. Without `FRED_API_KEY`, set `RISK_FREE_RATE_OVERRIDE=0.042`, and the run is flagged. A minimal offline-ish backend `.env` sets `DATABASE_URL=DATABASE_POOLER_URL=postgresql+psycopg://postgres@localhost:5432/postgres`, `REDIS_URL=redis://localhost:6379`, `STORAGE_BACKEND=local`, `DEV_AUTH_BYPASS=true` and `RISK_FREE_RATE_OVERRIDE=0.042`. EDGAR and Yahoo Finance still need internet access.
- Tests never need any of this. See `CLAUDE.md`: unit tests are offline, and integration tests use `TEST_DATABASE_URL` and `TEST_REDIS_URL`.

### Offline demo mode (`DATA_SOURCE_MODE=fixtures`)

Use this for a laptop with no internet access, or no SEC, Yahoo or FRED access, or for a demo that must be repeatable. Set `DATA_SOURCE_MODE=fixtures` in the environment of **both the worker and the API**. Nothing else changes: the database, Redis, jobs, Excel and PDF all work the same.

| Source | Live (`live`, the default) | Demo (`fixtures`) |
|---|---|---|
| SEC EDGAR | data.sec.gov / www.sec.gov | the bundled fixture JSON in `backend/app/data/demo/edgar/` (20 tickers), served in-process by `FixtureEdgarClient` |
| Price / shares | Yahoo Finance | fixed values per ticker (`DEMO_PRICES` in `app/data/demo/__init__.py`) |
| Risk-free rate | FRED DGS10 | fixed 4.20% |
| Damodaran | S3 cache, else the bundled snapshot | the bundled snapshot (S3 is never read) |
| Anthropic | used if `ANTHROPIC_API_KEY` is set | same (leave it empty for a fully offline run) |

Demo tickers:
- FCFF: AAPL, MSFT, GOOGL, WMT, NUE, PFE, DUK, SNOW
- excess return: JPM, TRV
- REIT NAV: O
- E&P NAV: EOG
- SOTP: HON
- declines: VKTX (pre-commercial biotech), ALAB (insufficient history), TSM (20-F filer), MET (life insurer), EPD (MLP), NEM (mining), CVII (SPAC)

Any other ticker fails with "not found". The fixtures are hand-built in SEC's formats from the companies' public filings. Headline figures are close to the real ones, and secondary lines are approximations. **They are not live data.**

Every demo run is marked so it cannot be mistaken for a real valuation:
- The first data-confidence flag of the result (Excel, PDF and result page) reads **"DEMO DATA — not live filings/prices"**, and so does the first classification reason on the confirm screen. A second flag names the fixed risk-free rate and prices.
- The filing accession is prefixed `DEMO-`, so demo builds never share cache entries (`cached_models`, `cached_proposals`) with live runs, even on the same database.

A fully offline backend `.env` for a demo:

```bash
DATABASE_URL=postgresql+psycopg://postgres@localhost:5432/postgres
DATABASE_POOLER_URL=postgresql+psycopg://postgres@localhost:5432/postgres
REDIS_URL=redis://localhost:6379
STORAGE_BACKEND=local
DEV_AUTH_BYPASS=true
DATA_SOURCE_MODE=fixtures        # no FRED_API_KEY / RISK_FREE_RATE_OVERRIDE / ANTHROPIC_API_KEY needed
```

Then run the processes as in Option B, open http://localhost:3000 and enter a ticker such as `AAPL`, `HON` or `JPM`. **Never** set `DATA_SOURCE_MODE=fixtures` in a deployed environment. The default is `live`, and `docker-compose.prod.yml` pins `DATA_SOURCE_MODE=live`.

To add or change a demo ticker:
1. Edit `backend/tests/fixtures/edgar/build_fixtures.py`.
2. Run `cd backend && .venv/bin/python -m tests.fixtures.edgar.build_fixtures`. It writes into `app/data/demo/edgar/`, the one canonical copy that the tests also read.
3. Add a price to `DEMO_PRICES`.
