# Deployment runbook

This is the runbook for taking the DCF app from a laptop to production. It condenses SPEC §11, §12, §14 and Ticket 15 and applies the decisions below. Most of the AWS work is scripted. The steps that need a human are logins, consoles, API keys and billing, and they are marked **[OWNER]**. Steps marked **[SCRIPTED]** are one command.

## Architecture and decisions

```
 browser ──HTTPS──> Vercel (Next.js frontend, no server secrets)
    │
    └──HTTPS──> CloudFront (*.cloudfront.net)  ──HTTP──>  ALB :80  ──>  ECS service dcf-api  (0.5 vCPU / 1 GB)
                  (or ALB :443 with your own ACM cert)                     │
                                                                           ├─> redis.dcf.internal:6379 ──> ECS service dcf-redis (0.25 / 0.5 GB)
                                           ECS service dcf-worker (1 vCPU / 2 GB) ─┘
                                           api + worker ──> Supabase Postgres (session pooler), S3, SEC EDGAR, Yahoo, FRED, Anthropic
```

| Decision | Choice | Why / alternative |
|---|---|---|
| Frontend hosting | **Vercel** (SPEC §14.8) | The frontend has no server secrets. Vercel gives HTTPS, previews and git-push deploys for free. The ECS path still works: `infra/docker/Dockerfile.frontend` is kept, and the alternative is described [below](#alternative-frontend-on-ecs). |
| Redis | **`redis:7-alpine` as a third Fargate service**, found through Cloud Map at `redis.dcf.internal` | Costs about $9/mo, where ElastiCache costs more. Redis has no persistence on purpose: a restart loses queued jobs, locks and rate-limiter state. Postgres and S3 remain the sources of truth (SPEC §11.3). The image comes from `public.ecr.aws/docker/library/redis`, which avoids Docker Hub pull limits. |
| Secrets | **SSM Parameter Store SecureString** under `/dcf/prod/*` | Standard parameters are free. ECS injects them through the task definition's `secrets` block, so nothing lands in an image or in git. |
| Network | **Default VPC, public subnets, public IPs, no NAT** | A NAT gateway costs about $33/mo or more. The worker's security group allows **no inbound traffic at all**, so it only makes outbound connections. Redis accepts traffic on :6379 only from the api and worker SGs. The api accepts traffic on :8000 only from the ALB SG. |
| API HTTPS | **CloudFront in front of the ALB** by default. `API_HTTPS_MODE=acm` gives the ALB an HTTPS listener if you own a domain. | The Vercel page is HTTPS, and browsers block calls from it to an `http://` API as mixed content. CloudFront provides a free `*.cloudfront.net` certificate. Caching is disabled, all headers except `Host` are forwarded, compression is off so SSE streams live, and the ALB accepts only CloudFront's origin-facing IP ranges. **Trade-off:** traffic between CloudFront and the ALB is plain HTTP over AWS's network. For anything beyond friends and family, register a cheap domain and switch to `acm`, which gives TLS end to end. |
| CI deploys | **GitHub Actions → OIDC role** (`AWS_ROLE_ARN` secret). No static AWS keys. | The trust policy allows only `repo:<owner>/<repo>:ref:refs/heads/main`. |
| Region | `us-east-1` (override with `AWS_REGION`) | |

## Checklist

| # | Step | Who |
|---|---|---|
| 1 | Create an AWS account and a **budget alarm first** | **[OWNER]** |
| 2 | Create a Supabase project, confirm JWT signing keys, copy keys and both connection strings, configure auth | **[OWNER]** |
| 3 | Create an Anthropic API key and confirm model access | **[OWNER]** |
| 4 | Get a FRED API key | **[OWNER]** |
| 5 | Set the SEC EDGAR User-Agent (just a string) | **[OWNER]** (copy/paste) |
| 6 | Fill in the local `.env` and `infra/deploy.env` | **[OWNER]** |
| 7 | Run DB migrations against Supabase | **[SCRIPTED]** `alembic upgrade head`, which deploy.sh also runs |
| 8 | AWS bootstrap: ECR, S3 and its lifecycle, logs, cluster, SGs, Cloud Map, IAM, ALB, CloudFront, GitHub OIDC role | **[SCRIPTED]** `infra/scripts/bootstrap_aws.sh` (the owner runs it with admin credentials) |
| 9 | Copy secrets into SSM | **[SCRIPTED]** `infra/scripts/put_ssm_params.sh` |
| 10 | Seed the Damodaran datasets into S3 | **[SCRIPTED]** `infra/scripts/seed_damodaran_cache.py`, with a manual browser-download fallback |
| 11 | First deploy: images, migrations, api/worker/redis services | **[SCRIPTED]** `infra/scripts/deploy.sh` |
| 12 | Add GitHub secret `AWS_ROLE_ARN` and repo variables, including `DEPLOY_ENABLED=true` | **[OWNER]** (GitHub settings) |
| 13 | Import the repo into Vercel and set 3 env vars | **[OWNER]** (Vercel dashboard) |
| 14 | Supabase Auth: set Site URL and redirect URLs to the Vercel domain | **[OWNER]** |
| 15 | Set `CORS_ORIGINS` to the Vercel domain, then redeploy | **[OWNER]** sets the value; **[SCRIPTED]** redeploy |
| 16 | Smoke test: sign in, run a real ticker, download the `.xlsx` and `.pdf` | **[OWNER]** |
| 17 | After that, every push to `main` redeploys the backend (GitHub Actions) and the frontend (Vercel) | automatic |

Tools you need on the machine that runs the scripts: AWS CLI v2, Docker (with BuildKit), git, python3, and ideally `envsubst` from the `gettext` package. The scripts fall back to python if `envsubst` is missing. On Apple Silicon, images are built with `--platform linux/amd64`, which is slower under emulation but correct.

---

## 1. AWS account and budget alarm [OWNER], first

AWS accounts created on or after 2025-07-15 are on the **credit-based Free Plan**. You get $100 of credit, plus up to $100 more from onboarding tasks. The credit expires after 6 months or when it runs out. **Fargate, ALB and ElastiCache are not Always-Free**, so every hour they run draws down that credit (SPEC §14.2). The [cost table](#monthly-cost-estimate) shows about $70–100/month for this stack.

1. Go to **Billing and Cost Management → Budgets → Create budget → Monthly cost budget**. Set it to about $100, or to the figure you are willing to spend. Add email alerts at 50%, 80% and 100% of actual cost, plus 100% of forecasted cost. (The $20 example in SPEC §14.2 would fire in the first week with this stack.)
2. Optionally, turn on **Cost Anomaly Detection**, which is free.
3. For local work, sign in with IAM Identity Center or an admin profile (`aws configure sso`). Do not create long-lived root keys. Production tasks use IAM roles and never static keys.

## 2. Supabase [OWNER]

1. Create a project at supabase.com. The free tier is enough. Choose region **us-east-1** so it sits next to Fargate, and save the database password.
2. Under **Project Settings → Data API**, copy the Project URL into `SUPABASE_URL`. The frontend's `NEXT_PUBLIC_SUPABASE_URL` uses the same value.
3. Under **Project Settings → API Keys**:
   - Copy the **publishable/anon** key into `SUPABASE_ANON_KEY`. The frontend's `NEXT_PUBLIC_SUPABASE_ANON_KEY` uses the same key, which is safe to expose to browsers.
   - Copy the **secret/service_role** key into `SUPABASE_SERVICE_ROLE_KEY`. It is for the backend only. Never put it in Vercel.
4. Under **Project Settings → JWT Keys**, confirm that the current signing key is **asymmetric** (ECC P-256 / RS256) and not the legacy HS256 shared secret. If the project still uses the legacy secret, click **Migrate JWT secret**, then **rotate** to the asymmetric key. Do this **before** any real users sign in, because rotating invalidates existing sessions. There is nothing to copy: the backend derives the JWKS URL, `{SUPABASE_URL}/auth/v1/.well-known/jwks.json`.
5. Click **Connect** at the top of the dashboard to get both connection strings. Change the prefix of each from `postgresql://` to `postgresql+psycopg://`:
   - **Direct connection** (`db.<ref>.supabase.co:5432`) goes in `DATABASE_URL`. SPEC §9.2 reserves it for Alembic only.
   - **Session pooler** (`aws-0-<region>.pooler.supabase.com:5432`, user `postgres.<ref>`) goes in `DATABASE_POOLER_URL`, which the running api and worker use. Do **not** use the transaction pooler on port 6543.
   - **IPv6 caveat:** without the paid IPv4 add-on, Supabase's direct host is **IPv6-only**, and the Connect dialog labels it "Not IPv4 compatible". Fargate in the default VPC and many home networks are IPv4-only. The session pooler handles DDL fine, so in that case:
     - For production, run `put_ssm_params.sh --migrations-via-pooler`. It stores the pooler string as `/dcf/prod/DATABASE_URL`, and the ECS migration task uses it.
     - For your laptop, if `alembic upgrade head` can't reach `db.<ref>.supabase.co`, run `alembic -x dburl="$DATABASE_POOLER_URL" upgrade head`.
6. Under **Authentication → Sign In / Providers**, enable Email (magic link or password, your choice). Either invite the ~10 users under **Authentication → Users → Invite**, or leave sign-ups open.
7. Under **Authentication → URL Configuration**, set **Site URL** to the Vercel production URL once you have it (step 13) and add it to **Redirect URLs**. Add `http://localhost:3000/**` there too for local dev.
8. Migrations: `deploy.sh` runs `alembic upgrade head` as a one-off ECS task before every rollout. To run them from your laptop first, use `cd backend && .venv/bin/alembic upgrade head`, which reads `DATABASE_URL` from `../.env`.
9. Sanity check in the SQL editor: `select policyname, tablename from pg_policies where schemaname = 'public';` should list the RLS policies from SPEC §3.

## 3. Anthropic [OWNER]

1. At console.anthropic.com, go to **API Keys → Create key** and copy it into `ANTHROPIC_API_KEY`.
2. Confirm that `claude-sonnet-5`, or whatever you set as `ANTHROPIC_MODEL`, is enabled for the org and supports structured outputs. Set a monthly spend limit under **Limits**.

## 4. FRED [OWNER]

Create a free account at fred.stlouisfed.org, request a key at fred.stlouisfed.org/docs/api/api_key.html, and put it in `FRED_API_KEY`. There is no keyless tier.

## 5. SEC EDGAR [OWNER]

There is no account or key. Set `SEC_EDGAR_USER_AGENT` to a real app name and contact email, for example `DCF-Valuation-App aman.todi01@gmail.com`. Requests without a compliant User-Agent, or bursts over 10 req/s, get a 403 or an IP block. The app's Redis token bucket prevents this, so don't bypass it.

## 6. Local config files [OWNER]

```bash
cp .env.example .env                              # real values from steps 2-5; git-ignored
cp infra/deploy.env.example infra/deploy.env      # SUPABASE_URL, CORS_ORIGINS, ...; git-ignored
```

In `.env`, `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` are only for local dev. You can leave them empty if your shell already has an AWS profile. Never put them in SSM. ECS uses the task role.

## 7. AWS bootstrap [SCRIPTED]

```bash
aws sts get-caller-identity          # confirm the right account, using admin credentials
infra/scripts/bootstrap_aws.sh
```

The script is idempotent. It checks for each resource before creating it and re-applies policies on every run. It creates:

| Resource | Name |
|---|---|
| ECR repos (scan on push, keep last 15 images) | `dcf-api`, `dcf-worker` |
| S3 bucket: public access blocked, SSE-S3, lifecycle from `infra/s3-lifecycle.json` (`models/` and `runs/` expire after 35 days; `edgar-raw/` and `damodaran/` are kept) | `dcf-app-artifacts-<account-id>` unless `S3_BUCKET_NAME` is set |
| CloudWatch log group (30-day retention, one stream prefix per container) | `/ecs/dcf` |
| ECS cluster (FARGATE + FARGATE_SPOT providers, Container Insights off) | `dcf-cluster` |
| Security groups in the default VPC | `dcf-alb-sg`, `dcf-api-sg`, `dcf-worker-sg`, `dcf-redis-sg` |
| Cloud Map private DNS namespace and service | `redis.dcf.internal` |
| IAM execution role: ECR pull on the 2 repos, logs on `/ecs/dcf`, `ssm:GetParameters` on `/dcf/prod/*` | `dcf-ecs-execution` |
| IAM task role: S3 List/Get/Put/Delete on the bucket only | `dcf-ecs-task` |
| ALB, target group (`/api/health`, ip targets on :8000), listener(s) | `dcf-alb`, `dcf-api-tg` |
| CloudFront distribution (default `API_HTTPS_MODE=cloudfront`) | comment `dcf-api` |
| GitHub OIDC provider and deploy role: ECR push, ECS register/update/run-task on `dcf-*`, PassRole on the two task roles only | `dcf-github-deploy` |

The IAM documents are in `infra/iam/*.json`. Edit them there and re-run the script. Networking uses 2 default subnets (`SUBNET_COUNT`) and skips `use1-az3`, where Fargate is not offered. Setting `SUBNET_IDS` overrides the subnet choice.

When it finishes, the script prints the **API base URL** (use it for Vercel), the **deploy role ARN** (use it for GitHub), and the remaining manual steps. A new CloudFront distribution takes about 5–15 minutes to reach every edge location.

If you use your own domain (`API_HTTPS_MODE=acm`):
1. Request a public certificate in ACM in the same region, for example `api.example.com`, and validate it through DNS.
2. Set `ACM_CERT_ARN` and re-run the bootstrap.
3. Point a CNAME, or a Route 53 alias, at the ALB DNS name.

## 8. Secrets into SSM [SCRIPTED]

```bash
infra/scripts/put_ssm_params.sh --env-file .env --dry-run                 # checks, writes nothing
infra/scripts/put_ssm_params.sh --env-file .env [--migrations-via-pooler] # see the IPv6 caveat in step 2.5
```

The script writes these SecureString parameters: `/dcf/prod/{SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY, DATABASE_URL, DATABASE_POOLER_URL, ANTHROPIC_API_KEY, FRED_API_KEY}`.
- It never prints values, and it passes them through a 0600 temp file rather than argv.
- It refuses to run if any key still has an `.env.example` placeholder or a DB URL is missing the `+psycopg` prefix.
- Plain settings (`SUPABASE_URL`, `CORS_ORIGINS`, `SEC_EDGAR_USER_AGENT`, `ANTHROPIC_MODEL`, the bucket, `REDIS_URL`, and `STORAGE_BACKEND=s3` / `DEV_AUTH_BYPASS=false`) go into the task definitions as ordinary `environment` entries, from `infra/deploy.env` or the GitHub variables.

To rotate a secret, re-run the script, then run `infra/scripts/deploy.sh` or `aws ecs update-service --cluster dcf-cluster --service dcf-api --force-new-deployment` (and the same for `dcf-worker`).

## 9. Damodaran datasets [SCRIPTED, with manual fallback]

```bash
cd backend
.venv/bin/python ../infra/scripts/seed_damodaran_cache.py --dry-run          # parse only
.venv/bin/python ../infra/scripts/seed_damodaran_cache.py --bucket dcf-app-artifacts-<account-id>
```

This uses your local AWS credentials. If pages.stern.nyu.edu blocks the script:
1. Download `betas.xls`, `margin.xls` and `histimpl.xls` in a browser.
2. Add `--from-local-dir ~/Downloads/damodaran`.

Re-run the script in January, when Damodaran republishes, and after any mid-year update. There is no automatic refresh, by design.

## 10. First deploy [SCRIPTED]

```bash
infra/scripts/deploy.sh
```

The deploy runs in this order:
1. **Preflight.** It checks that the cluster, all `/dcf/prod/*` parameters, the SGs and the target group exist.
2. **Images.** It builds `dcf-api` and `dcf-worker` for `linux/amd64` and pushes them to ECR, tagged with the git SHA. A tag already in ECR is not rebuilt. The script refuses to run on a dirty tree unless `ALLOW_DIRTY=true`.
3. **Task definitions.** It renders `infra/ecs/task-def-{api,worker,redis}.json` with `envsubst` and registers new revisions. These files contain `${PLACEHOLDER}` tokens and never real ARNs or secrets.
4. **Redis.** It creates `dcf-redis` with its Cloud Map registration the first time only. Later deploys leave it running. `DEPLOY_REDIS=true` forces a new revision, which drops the queue.
5. **Migrations.** It runs `alembic upgrade head` as a one-off Fargate task, using the **new api image** with a command override, and prints the log tail. **If it fails, the services are not touched.** `SKIP_MIGRATIONS=true` skips this step.
6. **Services.** It creates or updates `dcf-api` (behind the ALB, 60 s health-check grace) and `dcf-worker`. Both use rolling 100/200% deploys with the ECS circuit breaker and automatic rollback. The script then waits until the new revision is PRIMARY and `COMPLETED`. A rollback counts as a failed deploy.

Containers have `stopTimeout: 120`, the Fargate maximum, and the ALB deregistration delay is 60 s. Together these let SSE streams and in-flight builds drain on SIGTERM (SPEC §8.3).

**Keep migrations backward-compatible.** The old api and worker keep running against the new schema until the rollout finishes, so use expand-then-contract changes.

Verify with `curl https://<api-base-url>/api/health`, which should return `{"status":"ok"}`. Logs are in CloudWatch under `/ecs/dcf`, with streams `api/…`, `worker/…` and `redis/…`. You can also run `aws logs tail /ecs/dcf --follow`.

## 11. GitHub Actions [OWNER]

Go to **Settings → Secrets and variables → Actions**.

| Kind | Name | Value |
|---|---|---|
| Secret | `AWS_ROLE_ARN` | `arn:aws:iam::<account-id>:role/dcf-github-deploy` (printed by the bootstrap) |
| Variable | `DEPLOY_ENABLED` | `true`. While unset, `deploy.yml` skips with a notice, so pushes to main stay green |
| Variable | `SUPABASE_URL` | `https://<ref>.supabase.co` |
| Variable | `CORS_ORIGINS` | `https://<your-app>.vercel.app`. Comma-separate multiple origins; no trailing slash |
| Variable | `AWS_REGION` | optional, default `us-east-1` |
| Variable | `S3_BUCKET_NAME` | optional; set it if you overrode the default bucket name |
| Variable | `SEC_EDGAR_USER_AGENT` | optional; the default is the value in `.env.example` |
| Variable | `ANTHROPIC_MODEL` | optional, default `claude-sonnet-5` |
| Variable | `USE_FARGATE_SPOT` | optional, `true` to create worker and redis on Spot (applied only when the services are first created) |

`.github/workflows/deploy.yml` runs on every push to `main`, and on demand through **Run workflow** from `main`. It:
1. Assumes the role through OIDC.
2. Runs `infra/scripts/deploy.sh` with `IMAGE_TAG=${{ github.sha }}`.
3. Uses a single concurrency group, so deploys never overlap.

The role's trust policy accepts only the `main` branch of this repo. The deploy does not wait for `ci-backend` to pass, so protect `main` with required status checks.

## 12. Frontend on Vercel [OWNER]

1. At vercel.com, go to **Add New → Project** and import the GitHub repo.
2. Set **Root Directory** to `frontend`. Next.js is auto-detected. The `output: "standalone"` setting in `next.config.ts` is harmless on Vercel.
3. Add these environment variables for Production, and for Preview too if you want previews to work:
   - `NEXT_PUBLIC_SUPABASE_URL` = `SUPABASE_URL`
   - `NEXT_PUBLIC_SUPABASE_ANON_KEY` = the publishable/anon key. It is public by design. **Never** add the service role key here.
   - `NEXT_PUBLIC_API_BASE_URL` = the **https** API base URL printed by the bootstrap (CloudFront or your ACM domain), with no trailing slash.
4. Deploy, then note the production URL (for example `https://dcf-valuation.vercel.app`).
5. Put that exact origin in `CORS_ORIGINS`, in both the GitHub variable and `infra/deploy.env`, and redeploy the backend. Also add it to Supabase **Site URL / Redirect URLs** (step 2.7).
6. Preview deployments get random URLs that aren't in `CORS_ORIGINS`, so their API calls fail CORS. That is expected. Test against production, or add a stable preview alias.

`NEXT_PUBLIC_*` values are baked in at build time, so changing them in Vercel requires a redeploy.

### Alternative: frontend on ECS

Use this path only if you would rather not use Vercel.
1. Create an ECR repo `dcf-frontend`.
2. Build `infra/docker/Dockerfile.frontend` with `--build-arg NEXT_PUBLIC_API_BASE_URL=… NEXT_PUBLIC_SUPABASE_URL=… NEXT_PUBLIC_SUPABASE_ANON_KEY=…`. These values are baked in at build time.
3. Register a task definition modeled on `task-def-api.json` with port 3000, no secrets and no task role, at 0.25 vCPU / 0.5 GB.
4. Add a second target group plus an ALB listener rule: `/api/*` goes to `dcf-api-tg` and the default goes to the frontend TG.
5. The page and the API then share one origin, so `NEXT_PUBLIC_API_BASE_URL` can be the same HTTPS host.

This adds about $9/mo in Fargate and $3.65/mo for another public IPv4. It is not scripted.

## 13. Smoke test [OWNER]

1. Sign in on the Vercel URL.
2. Run a plain FCFF name such as `AAPL`: confirm the proposed assumptions, then build.
3. The progress screen should stream live over SSE. If updates arrive only at the end, CloudFront is buffering. Switch to `acm`.
4. Download the `.xlsx` and `.pdf`. The presigned S3 links expire after a short time. Check that the workbook recalculates in Excel.
5. Cancel a run mid-build to confirm it moves to `cancelled`.

---

## Monthly cost estimate

This estimate is for us-east-1 with on-demand Linux/x86 Fargate at $0.04048 per vCPU-hour and $0.004445 per GB-hour, running 730 hours a month, with tiny traffic. Check current prices before you commit.

| Item | Size | ≈ $/month |
|---|---|---|
| Fargate `dcf-api` | 0.5 vCPU / 1 GB | 18.0 |
| Fargate `dcf-worker` | 1 vCPU / 2 GB (LibreOffice + WeasyPrint) | 36.0 |
| Fargate `dcf-redis` | 0.25 vCPU / 0.5 GB | 9.0 |
| Application Load Balancer | $0.0225/h plus under 1 LCU | 17–19 |
| Public IPv4 addresses | 3 tasks + 2 ALB nodes × $0.005/h | 18.3 |
| CloudFront | always-free tier (1 TB, 10 M requests) | 0 |
| Cloud Map (Route 53 private zone + 1 instance) | | ~0.6 |
| CloudWatch Logs (30-day retention) | low volume | ~1 |
| ECR storage (15 images per repo, shared layers) | ~2–4 GB | ~0.3 |
| S3, SSM standard parameters, OIDC | | ~0 |
| **Total AWS** | | **≈ $100** |
| with `USE_FARGATE_SPOT=true` (worker + redis on Spot, about 70% off) | | ≈ $70 |
| Supabase (free), Vercel Hobby (free), FRED/EDGAR/Damodaran (free) | | 0 |
| Anthropic | per token, for about 10 users | usage-based; set a limit |

Levers, from biggest saving to smallest:
- Fargate Spot for the worker and redis. The 2-minute interruption notice matches the SIGTERM drain.
- `aws ecs update-service --desired-count 0` for all three services when nobody is using the app. The ALB and its IPv4 addresses still bill while the services are scaled down.
- Smaller worker memory, if LibreOffice fits.

The $100–200 of Free Plan credit lasts about 1–2 months at this rate.

## Operations cheat sheet

```bash
aws logs tail /ecs/dcf --follow --log-stream-name-prefix api        # or worker / redis
aws ecs describe-services --cluster dcf-cluster --services dcf-api dcf-worker dcf-redis \
  --query 'services[].{svc:serviceName,run:runningCount,td:taskDefinition,events:events[0].message}'
# Roll back: redeploy an older commit (images are tagged with the full 40-char SHA; last 15 kept)
git checkout <old-sha> && SKIP_MIGRATIONS=true infra/scripts/deploy.sh   # reuses the image already in ECR
# Pause everything (the ALB still bills)
for s in dcf-api dcf-worker dcf-redis; do aws ecs update-service --cluster dcf-cluster --service $s --desired-count 0 >/dev/null; done
```

Note on rollback: `deploy.sh` renders the task definitions from the current checkout, which is why the rollback command runs it from the old commit. The ECS circuit breaker already rolls back a deploy that never becomes healthy on its own.

---

## Local development

### Option A: docker compose

```bash
cp .env.example .env     # fill in real dev values; DATABASE_*_URL can point at Supabase or at a local Postgres
docker compose up --build   # redis :6379, api :8000, worker, frontend :3000
```

Compose overrides `REDIS_URL` to point at the redis container. Inside the containers, `localhost` means the container itself. To use a Postgres running on the host, set the URLs to `host.docker.internal` on macOS or Windows. On Linux, add `extra_hosts: ["host.docker.internal:host-gateway"]`.

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
- With local Postgres, Supabase Auth is not present. Either keep `SUPABASE_URL` and the anon key pointing at a real Supabase project, which only handles sign-in and JWKS, or set `DEV_AUTH_BYPASS=true`: the frontend (with `NEXT_PUBLIC_SUPABASE_URL` unset) sends `Bearer dev-bypass-token`, which the API maps to a fixed dev user it inserts into the stub `auth.users`. **Never** enable it in production: the ECS task definitions pin `DEV_AUTH_BYPASS=false`.
- `STORAGE_BACKEND=local` (+ `LOCAL_STORAGE_DIR`, `PUBLIC_API_BASE_URL`) writes artifacts to disk instead of S3; download links become signed, expiring `GET /api/files/...` URLs on the API. Production pins `s3`.
- No keys at all: without `ANTHROPIC_API_KEY` proposals and report prose use the deterministic fallback; without `FRED_API_KEY` set `RISK_FREE_RATE_OVERRIDE=0.042` (the run is flagged). A minimal offline-ish backend `.env`:
  `DATABASE_URL=DATABASE_POOLER_URL=postgresql+psycopg://postgres@localhost:5432/postgres`, `REDIS_URL=redis://localhost:6379`, `STORAGE_BACKEND=local`, `DEV_AUTH_BYPASS=true`, `RISK_FREE_RATE_OVERRIDE=0.042` (EDGAR and Yahoo Finance still need internet access).
- Tests never need any of this. See `CLAUDE.md`: unit tests are offline, and integration tests use `TEST_DATABASE_URL` and `TEST_REDIS_URL`.

### Offline demo mode (`DATA_SOURCE_MODE=fixtures`)

For a laptop with no internet access (or no SEC/Yahoo/FRED access), or for a demo that must be
repeatable, set `DATA_SOURCE_MODE=fixtures` in the **worker's and the API's** environment. Nothing
else changes: same database, Redis, jobs, Excel and PDF.

| Source | Live (`live`, the default) | Demo (`fixtures`) |
|---|---|---|
| SEC EDGAR | data.sec.gov / www.sec.gov | the bundled fixture JSON in `backend/app/data/demo/edgar/` (20 tickers), served in-process by `FixtureEdgarClient` |
| Price / shares | Yahoo Finance | fixed values per ticker (`DEMO_PRICES` in `app/data/demo/__init__.py`) |
| Risk-free rate | FRED DGS10 | fixed 4.20% |
| Damodaran | S3 cache, else the bundled snapshot | the bundled snapshot (S3 is never read) |
| Anthropic | used if `ANTHROPIC_API_KEY` is set | same (leave it empty for a fully offline run) |

Demo tickers: AAPL, MSFT, GOOGL, WMT, NUE, PFE, DUK, SNOW (FCFF), JPM, TRV (excess return), O (REIT NAV),
EOG (E&P NAV), HON (SOTP), and the declines VKTX (pre-commercial biotech), ALAB (insufficient history),
TSM (20-F filer), MET (life insurer), EPD (MLP), NEM (mining), CVII (SPAC). Any other ticker fails with
"not found". The fixtures are hand-built in SEC's formats from the companies' public filings; headline
figures are close to the real ones, secondary lines are approximations. **They are not live data.**

Every demo run is marked so it cannot be mistaken for a real valuation:
- the first data-confidence flag of the result (Excel, PDF, result page) and the first classification
  reason on the confirm screen read **"DEMO DATA — not live filings/prices"**, plus a flag naming the
  fixed risk-free rate and prices;
- the filing accession is prefixed `DEMO-`, so demo builds never share cache entries (`cached_models`,
  `cached_proposals`) with live runs even on the same database.

A fully offline backend `.env` for a demo:

```bash
DATABASE_URL=postgresql+psycopg://postgres@localhost:5432/postgres
DATABASE_POOLER_URL=postgresql+psycopg://postgres@localhost:5432/postgres
REDIS_URL=redis://localhost:6379
STORAGE_BACKEND=local
DEV_AUTH_BYPASS=true
DATA_SOURCE_MODE=fixtures        # no FRED_API_KEY / RISK_FREE_RATE_OVERRIDE / ANTHROPIC_API_KEY needed
```

Then run the processes as in Option B, open http://localhost:3000 and enter e.g. `AAPL`, `HON` or `JPM`.
**Never** set `DATA_SOURCE_MODE=fixtures` in a deployed environment: the default is `live`, and the ECS
task definitions pin `DATA_SOURCE_MODE=live`.

To add or change a demo ticker, edit `backend/tests/fixtures/edgar/build_fixtures.py`, run
`cd backend && .venv/bin/python -m tests.fixtures.edgar.build_fixtures` (it writes into
`app/data/demo/edgar/`, the one canonical copy the tests also read), and add a price to `DEMO_PRICES`. See `CLAUDE.md`: unit tests are offline, and integration tests use `TEST_DATABASE_URL` and `TEST_REDIS_URL`.
