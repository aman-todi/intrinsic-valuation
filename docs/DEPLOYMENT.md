# Deployment runbook

This runbook takes the DCF app from a laptop to production at the lowest cost that works for v1, about 20–50 users. Everything except the frontend runs in **one AWS account, provisioned by Terraform**: the backend runs on **one Graviton EC2 instance with docker compose**, Postgres is **Amazon RDS for PostgreSQL** (private, single-AZ), sign-in is **Amazon Cognito** (managed login), and artifacts are in **S3**. The **frontend is on Vercel**.

The runbook has two parts:
- **[Part A: Manual steps (account owner)](#part-a-manual-steps-account-owner)** covers accounts, consoles, API keys, users, DNS and the GitHub/Vercel settings.
- **[Part B: Scripted / automated](#part-b-scripted--automated)** covers everything that is one command, or that runs by itself on every push to `main`.

Use the [order of operations](#order-of-operations) table to move between the two parts.

## Architecture

```
 browser ──HTTPS──> Vercel (Next.js frontend; NEXT_PUBLIC_* only, no server secrets)
    │  └──redirect (Authorization Code + PKCE)──> Cognito managed login  https://<prefix>.auth.<region>.amazoncognito.com
    │                                             (user pool dcf-users; invite-only by default; password or email code)
    │
    └──HTTPS (CORS, Authorization: Bearer <Cognito access token>)
         ──> EC2 t4g.small, Elastic IP, Amazon Linux 2023 arm64   (security group dcf-app: 80/443 only)
              └─ docker compose  (/opt/dcf/docker-compose.prod.yml)
                   caddy :80/:443  auto HTTPS (Let's Encrypt) for <api host>, SSE flushed immediately
                     └─> api :8000     uvicorn; validates access tokens against the pool's JWKS
                   worker            SAQ + LibreOffice + WeasyPrint
                   redis :6379       queue / locks / rate limiter / SSE fan-out, no persistence
              api + worker ──TLS──> RDS PostgreSQL 17, db.t4g.micro, private, same AZ
                                    (security group dcf-db: 5432 from dcf-app only)
              api + worker ──> S3 (instance role), SEC EDGAR, Yahoo, FRED, Anthropic

 GitHub Actions (push to main) ──OIDC──> ECR push (arm64 images) ──> SSM Run Command ──> /opt/dcf/deploy.sh <sha>
```

| Decision | Choice | Why / alternative |
|---|---|---|
| Compute | **One EC2 `t4g.small`** (2 vCPU burstable, 2 GB RAM, plus a 2 GB swapfile), arm64 | About $12/month. The whole backend (api, worker, redis, caddy) fits. `t4g.medium` (4 GB) is the upgrade path, [below](#upgrade-the-instance). There is no high availability: a host failure is auto-recovered by EC2, and a deploy restarts the containers, which takes a few seconds. |
| Database | **RDS for PostgreSQL 17**, `db.t4g.micro`, 20 GB gp3 (autoscaling to 50 GB), single-AZ, encrypted, not publicly accessible, TLS forced | About $14/month. Managed backups (7 days, point-in-time restore), minor-version patching and storage autoscaling. The only way in is from the app instance's security group; humans connect through an [SSM port forward](#connect-to-the-database). The cheaper alternative, Postgres in a container on the EC2 box (≈ $0 extra, but you own backups, upgrades and disk), is [described but not implemented](#cheaper-alternative-postgres-on-the-box). |
| Auth | **Cognito user pool** (Essentials tier) with **managed login v2**, public app client, Authorization Code + PKCE | No auth UI to build or host. Invite-only by default. Password sign-in, optional passwordless email codes, optional TOTP MFA. The API validates Cognito **access tokens** (issuer, signature via JWKS, `token_use=access`, `client_id`). Free for up to 10,000 monthly active users on Essentials at the time of writing ([cost](#monthly-cost)). |
| Frontend | **Vercel** (Hobby tier) | Free for personal, non-commercial use, and it gives HTTPS, previews and git-push deploys. The browser calls the API cross-origin, so the API allows the Vercel origin(s) through `CORS_ORIGINS`. |
| HTTPS for the API | **Caddy** with automatic Let's Encrypt | Caddy needs no load balancer and no certificate management. Without a domain, the hostname is `<eip-with-dashes>.sslip.io`, a public wildcard DNS name that resolves to the Elastic IP. A custom domain is [optional](#a7-custom-domains-optional). |
| Artifacts | **S3**, accessed with the **instance role** | There are no static keys. `models/` and `runs/` expire after 35 days. |
| Images | **ECR** (`dcf-api`, `dcf-worker`), tagged with the git SHA, last 10 kept | The instance pulls with its role. |
| Runtime config and secrets | **SSM Parameter Store** under `/dcf/prod/*` | Terraform writes the non-secret `config/*` values (Cognito ids, domain, bucket, …) and the `DATABASE_URL` SecureString. The owner writes the third-party API keys with `put_ssm_params.sh`; those never enter git, images or Terraform state. Each deploy renders `/opt/dcf/.env` (0600) from SSM. |
| Shell access | **SSM Session Manager** | No SSH port is open and there is no key pair. |
| Deploys | **GitHub Actions → OIDC → ECR → SSM Run Command** | No inbound access is needed for deploys. The same flow runs from a laptop with `infra/scripts/deploy.sh`. The deploy role can read `/dcf/prod/config/*` only, never the secrets or `DATABASE_URL`. |
| IaC | **Terraform** (`infra/terraform/`), S3 state with native lockfiles | No DynamoDB table is needed (Terraform ≥ 1.10). |

> **The database password is in Terraform state.** Terraform generates it (`random_password.db`), sets it on the RDS instance and writes it into the `/dcf/prod/DATABASE_URL` SecureString. Both the password and the full URL are therefore stored in the state file, which lives in the private, versioned, SSE-encrypted, public-access-blocked state bucket created by `infra/terraform/bootstrap`. Treat read access to that bucket (and to `terraform.tfstate` backups) as read access to the database.

What lives where:

| Path | What |
|---|---|
| `infra/terraform/bootstrap/` | The Terraform state bucket (one time) |
| `infra/terraform/` | VPC lookups, security groups, EIP, instance and user data, RDS (`database.tf`), Cognito (`auth.tf`), IAM (instance role, GitHub OIDC deploy role), S3, ECR, SSM parameters, budget, status-check alarms |
| `infra/deploy/docker-compose.prod.yml`, `infra/deploy/Caddyfile` | The production stack. It is shipped to the box with every deploy |
| `infra/scripts/deploy_remote.sh` | Runs **on the box** as `/opt/dcf/deploy.sh <sha>` |
| `infra/scripts/deploy.sh` | Builds and pushes images (optional), uploads the bundle, triggers the box through SSM. Used by CI and for manual deploys |
| `infra/scripts/put_ssm_params.sh` | Copies the owner's API keys from `.env` into SSM |
| `infra/scripts/seed_damodaran_cache.py` | Seeds the Damodaran datasets into S3 |
| `infra/docker/Dockerfile.{api,worker}` | Production images, linux/arm64 |
| `infra/docker/Dockerfile.frontend` | Only for the local `docker-compose.yml`. Production uses Vercel |

## Order of operations

| # | Step | Who | Section |
|---|---|---|---|
| 1 | AWS account, budget awareness, local tools | **Owner** | [A1](#a1-prerequisites) |
| 2 | Anthropic key and spend limit, FRED key, SEC User-Agent | **Owner** | [A2](#a2-anthropic-fred-sec-edgar) |
| 3 | Fill in `terraform.tfvars` (incl. `frontend_origins`, Cognito options) | **Owner** | [A3](#a3-choose-the-terraform-variables) |
| 4 | Terraform: state bucket, then `apply` (creates EC2, RDS, Cognito, …; RDS takes ~5–10 min) | **Scripted** (the owner runs it) | [B1](#b1-terraform) |
| 5 | API keys into SSM | **Scripted** | [B2](#b2-secrets-into-ssm) |
| 6 | GitHub secret and variables, `DEPLOY_ENABLED=true` | **Owner** | [A5](#a5-github-actions) |
| 7 | First deploy (push to `main`, or `deploy.sh`); it runs the migrations against RDS | **Automated** / **Scripted** | [B3](#b3-deploy) |
| 8 | Seed the Damodaran cache | **Scripted** | [B4](#b4-seed-the-damodaran-datasets) |
| 9 | Vercel project and env vars (from `terraform output vercel_env`), then `frontend_origins` → `apply` → redeploy API | **Owner** + **Scripted** | [A4](#a4-vercel-frontend) |
| 10 | Invite users | **Owner** | [A6](#a6-users-cognito) |
| 11 | Optional: custom API / frontend domain | **Owner** + **Scripted** | [A7](#a7-custom-domains-optional) |
| 12 | Smoke test, including sign-in through Cognito managed login | **Owner** | [A8](#a8-smoke-test) |
| — | After that, every push to `main` redeploys the API (Actions) and the frontend (Vercel) | automatic | |

---

# Part A: Manual steps (account owner)

## A1. Prerequisites

1. **AWS account.** Sign in with IAM Identity Center or an admin profile (`aws configure sso`). Never use root keys. The resources here cost about **$33–35/month** ([cost table](#monthly-cost)). New accounts on the credit-based Free Plan draw the credit down. EC2, RDS, EBS and public IPv4 are not Always-Free.
2. **Budget.** Terraform creates a monthly budget of **$45** (`monthly_budget_usd`), which alerts `alert_email` at 80% and 100% of actual spend and at 100% of forecast. Optionally, turn on Cost Anomaly Detection in the console. It is free.
3. **Local tools** on the machine that runs Terraform and the scripts:
   - Terraform **≥ 1.10** (tested with 1.16)
   - AWS CLI v2
   - the [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) for `aws ssm start-session` (shell and database port forwarding)
   - git and python3; `psql` (any recent client) if you want to open the database by hand
   - for manual image builds only: Docker with buildx. Apple Silicon builds arm64 natively. On x86 you need QEMU/binfmt, which Docker Desktop includes. On Linux, run `docker run --privileged --rm tonistiigi/binfmt --install arm64`.
4. **GitHub**: admin on the repository, for secrets and variables.
5. **Vercel** account, which can be created with GitHub login.

There is no database or auth provider account to create: RDS and Cognito come from Terraform.

## A2. Anthropic, FRED, SEC EDGAR

- **Anthropic.** At console.anthropic.com, go to **API Keys → Create key** and copy it into `ANTHROPIC_API_KEY`. Confirm that `ANTHROPIC_MODEL` (default `claude-sonnet-5`) is enabled for the org. **Set a monthly spend limit** under **Limits**.
- **FRED.** Get a free key at fred.stlouisfed.org/docs/api/api_key.html and put it in `FRED_API_KEY`.
- **SEC EDGAR.** There is no key. Set `SEC_EDGAR_USER_AGENT` to a real app name and contact email, for example `DCF-Valuation-App aman.todi01@gmail.com`. Non-compliant or bursty clients get a 403 or an IP block. The app's Redis token bucket keeps requests under 10 req/s.

Collect these in the git-ignored `.env`, starting from `cp .env.example .env`. `put_ssm_params.sh` reads only `ANTHROPIC_API_KEY`, `FRED_API_KEY`, `SEC_EDGAR_USER_AGENT` and `ANTHROPIC_MODEL` from it. `DATABASE_URL` and `COGNITO_*` in your `.env` are for local development only; production gets them from Terraform, and the script ignores them.

## A3. Choose the Terraform variables

Run `cp infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars` (the copy is git-ignored), then set:

| Variable | Value |
|---|---|
| `aws_region` | `us-east-1` (default). EC2, RDS and Cognito all live here |
| `github_owner` / `github_repo` | for example `aman-todi` / `intrinsic-valuation`. Only `refs/heads/main` of this repo can assume the deploy role |
| `alert_email` | budget alerts and the Let's Encrypt contact |
| `frontend_origins` | `[]` for now. After [A4](#a4-vercel-frontend), `["https://<your-app>.vercel.app"]`. This **one list** feeds the API's `CORS_ORIGINS` **and** the Cognito app client's callback URLs (`<origin>/auth/callback`) and sign-out URLs (`<origin>/`). Exact origins, `https://`, no trailing slash, no wildcards. (It replaces the former `cors_origins` variable; rename it in an existing `terraform.tfvars`.) |
| `cognito_allow_self_signup` | `false` (default): invite-only, users are created by you ([A6](#a6-users-cognito)). `true`: anyone can create an account on the managed login page |
| `cognito_email_otp_enabled` | `true` (default): the sign-in page offers "email me a code" next to the password. Every code is an email and counts toward the [50 emails/day](#email-sending-limits) limit; set `false` for password-only |
| `cognito_localhost_callbacks` | `true` (default): also allows `http://localhost:3000/auth/callback` and `http://localhost:3000/`, so a local frontend can sign in against this pool. Set `false` to lock the pool to the deployed origins |
| `cognito_domain_prefix` | optional; empty = `dcf-<random hex>` → `https://dcf-1a2b3c4d.auth.us-east-1.amazoncognito.com` |
| `db_instance_class` / `db_engine_version` | `db.t4g.micro` / `17` (defaults). `17` = the region's default 17.x minor, with automatic minor upgrades |
| `db_deletion_protection`, `cognito_deletion_protection` | `true` (defaults). Set `false` and apply only right before a [teardown](#teardown) |
| `domain_name` | optional; empty = `<eip>.sslip.io` |
| `instance_type` / `worker_concurrency` | `t4g.small` / `2` (the defaults) or `t4g.medium` / `4` |
| `monthly_budget_usd` | `45` (the default) |

Then run Part B, [B1–B2](#b1-terraform).

## A4. Vercel (frontend)

You need the Terraform outputs first:

```bash
terraform -chdir=infra/terraform output vercel_env
# {
#   NEXT_PUBLIC_API_BASE_URL         = "https://3-91-20-7.sslip.io"
#   NEXT_PUBLIC_COGNITO_CLIENT_ID    = "4abc…"
#   NEXT_PUBLIC_COGNITO_DOMAIN       = "https://dcf-1a2b3c4d.auth.us-east-1.amazoncognito.com"
#   NEXT_PUBLIC_COGNITO_REGION       = "us-east-1"
#   NEXT_PUBLIC_COGNITO_USER_POOL_ID = "us-east-1_AbCdEf123"
# }
```

1. At vercel.com, go to **Add New → Project** and import the GitHub repo.
2. Set **Root Directory** to `frontend`. Next.js is auto-detected. The `output: "standalone"` setting in `next.config.ts` is harmless on Vercel.
3. Add these environment variables for **Production**, and for **Preview** if you use previews. All five are public by design (the app client has no secret):
   - `NEXT_PUBLIC_API_BASE_URL` = `api_url` output, **https**, no trailing slash
   - `NEXT_PUBLIC_COGNITO_DOMAIN` = `cognito_domain_url` output
   - `NEXT_PUBLIC_COGNITO_CLIENT_ID` = `cognito_client_id` output
   - `NEXT_PUBLIC_COGNITO_USER_POOL_ID` = `cognito_user_pool_id` output
   - `NEXT_PUBLIC_COGNITO_REGION` = `cognito_region` output
4. Deploy, then note the production URL, for example `https://dcf-valuation.vercel.app`.
5. Register that origin with the API **and** with Cognito:
   1. In `terraform.tfvars`, set `frontend_origins = ["https://dcf-valuation.vercel.app"]`. Use the exact origin, with no trailing slash.
   2. Run `terraform apply`. It updates the SSM `CORS_ORIGINS` value and the Cognito app client's callback/sign-out URLs in place.
   3. **Redeploy the API**: re-run the latest `deploy` workflow, or run `infra/scripts/deploy.sh --skip-build --tag <current sha>`. The deploy re-renders `.env` and recreates the containers. (Cognito picks up the new URLs immediately; no frontend redeploy is needed.)
6. From now on, Vercel builds and deploys the frontend on every push to `main`, through its own Git integration. `NEXT_PUBLIC_*` values are inlined at build time, so after changing one in Vercel, redeploy the frontend.

**Preview deployments** get per-branch or per-commit URLs that are neither in `CORS_ORIGINS` nor in Cognito's callback list, so their API calls fail CORS and sign-in fails with `redirect_mismatch`. Neither CORS nor Cognito accepts wildcards here. You have two options:
- Test on production.
- Add a **stable** preview alias, such as a branch domain like `dcf-git-staging-<team>.vercel.app`, to `frontend_origins` (apply, redeploy the API).

The trade-off: every listed origin can receive sign-in redirects and make credentialed browser calls to the API. Each API call still needs a valid Cognito access token, because CORS is not authentication, but keep the list short.

**Vercel Hobby is free but limited to personal, non-commercial use.** Commercial use needs Pro, at $20 per user per month.

## A5. GitHub Actions

Go to **Settings → Secrets and variables → Actions**.

| Kind | Name | Value |
|---|---|---|
| Secret | `AWS_DEPLOY_ROLE_ARN` | `terraform output -raw github_deploy_role_arn` |
| Variable | `DEPLOY_ENABLED` | `true`. While unset, `deploy.yml` only posts a notice, so `main` stays green |
| Variable | `AWS_REGION` | optional, default `us-east-1`. Must match `aws_region` |
| Variable | `ARM_RUNNER` | optional, default `ubuntu-24.04-arm` (native arm64 GitHub-hosted runner). If arm64 hosted runners are not available for this repository or plan, so the job never starts, set it to `ubuntu-latest`. The images are then built under QEMU emulation, which is slower |

The workflow needs nothing else. It reads the instance id, bucket and registry from SSM `/dcf/prod/config/*`, which Terraform writes. The frontend's `NEXT_PUBLIC_*` values live in Vercel, not in GitHub.

Protect `main` with required status checks (`ci-backend`, `ci-frontend`). The deploy workflow does not wait for CI.

## A6. Users (Cognito)

The pool is **invite-only** by default (`cognito_allow_self_signup = false`). The username **is** the email address. An invited user gets an email with a temporary password (valid 7 days), signs in on the managed login page and must choose a new password (≥ 12 characters, upper and lower case and a digit). After that they can sign in with the password or, if `cognito_email_otp_enabled`, with a one-time code sent by email. Users can turn on an authenticator app (TOTP) as optional MFA. A user with TOTP turned on signs in with password + TOTP only; Cognito does not offer the passwordless email code to users who have MFA.

**CLI** (from `infra/terraform/`):

```bash
POOL="$(terraform output -raw cognito_user_pool_id)"
aws cognito-idp admin-create-user --user-pool-id "$POOL" \
  --username alice@example.com \
  --user-attributes Name=email,Value=alice@example.com Name=email_verified,Value=true \
  --desired-delivery-mediums EMAIL
# resend an expired invite:
aws cognito-idp admin-create-user --user-pool-id "$POOL" --username alice@example.com --message-action RESEND
```

**Console:** **Amazon Cognito → User pools → `dcf-users` → User management → Users → Create user**. Choose **Send an email invitation**, enter the email address, tick **Mark email address as verified**, choose **Generate a password**, and click **Create user**.

Users only need to exist in Cognito. There is nothing to create in the database: the API upserts a `users` row (keyed by the token's `sub`) on each user's first authenticated request.

Day-to-day user administration is in [Operations](#cognito-user-administration).

### Email sending limits

The pool sends email with Cognito's built-in sender (`COGNITO_DEFAULT`, from `no-reply@verificationemail.com`). It is limited to **50 emails per day** for the account. Invitations, verification codes, password resets and every passwordless sign-in code count toward it. That is enough for a 20–50 user pilot that mostly signs in with passwords; if you hit it (or want your own From address), move to Amazon SES:

1. In SES (same region), verify a domain or address and request production access (to leave the SES sandbox).
2. Change `email_configuration` in `infra/terraform/auth.tf` to `email_sending_account = "DEVELOPER"` with `source_arn` = the SES identity ARN and `from_email_address` = e.g. `DCF <no-reply@example.com>`, then `terraform apply`. (Not parameterized today.) SES costs $0.10 per 1,000 emails.

## A7. Custom domains (optional)

**API.** The sslip.io hostname works with no purchase. It depends on a free third-party DNS service and on Let's Encrypt limits shared with everyone else who uses sslip.io, although Caddy retries and falls back to ZeroSSL. For anything beyond a pilot, use a cheap domain (about $10/year):

1. At your DNS provider, create **`A api.example.com → <elastic_ip>`**, using `terraform output elastic_ip`. Wait until `dig +short api.example.com` returns the IP.
2. In `terraform.tfvars`, set `domain_name = "api.example.com"` and run `terraform apply`. This updates the SSM config (`APP_DOMAIN`, `PUBLIC_API_BASE_URL`) and does not touch the instance.
3. Redeploy the API. Caddy is recreated with the new hostname and gets a certificate within seconds. Check with `curl https://api.example.com/api/health`.
4. In Vercel, set `NEXT_PUBLIC_API_BASE_URL=https://api.example.com` and redeploy the frontend.

**Frontend.** Add the domain in Vercel (**Project → Domains**), then add its origin to `frontend_origins`, `terraform apply` (CORS + Cognito callbacks) and redeploy the API.

**Sign-in page.** The managed login page stays on `<prefix>.auth.<region>.amazoncognito.com`. A branded `auth.example.com` is possible (Cognito custom domain: an ACM certificate in **us-east-1**, an A record for the parent domain, and a CNAME to the CloudFront target Cognito returns) but is not implemented here; it would also change `NEXT_PUBLIC_COGNITO_DOMAIN`.

## A8. Smoke test

1. Run `curl https://<api host>/api/health`, which should return `{"status":"ok"}`. HTTP must redirect to HTTPS.
2. Run `curl -si https://<api host>/api/runs` without a token. It must be rejected (401).
3. Run `curl -si -X OPTIONS https://<api host>/api/runs -H 'Origin: https://<your-app>.vercel.app' -H 'Access-Control-Request-Method: POST' -H 'Access-Control-Request-Headers: authorization,content-type' | grep -i access-control-allow-origin`. It should echo the Vercel origin.
4. **Sign in** on the Vercel URL. You should be redirected to the Cognito managed login page (`https://<prefix>.auth.<region>.amazoncognito.com/…`), sign in with an invited user (first time: temporary password, then choose a new one; later: password or email code), and land back on the Vercel URL via `/auth/callback`, signed in. A `redirect_mismatch` error means the origin is missing from `frontend_origins` (A4 step 5).
5. **Sign out** returns to the Vercel home page, and signing in again asks for credentials.
6. Run a plain FCFF name such as **`AAPL`**: confirm the proposed assumptions, then build.
7. The progress screen should update **live** as steps complete, which is SSE through Caddy. If all updates arrive only at the end, something is buffering. Check the `flush_interval -1` line in `infra/deploy/Caddyfile`.
8. Download the **`.xlsx`** and the **`.pdf`**. The links are presigned S3 URLs that expire. Check that the workbook recalculates in Excel.
9. Start another run and **cancel** it mid-build. It should move to `cancelled`.
10. Optionally, redeploy while a build runs. The worker drains for up to 60 s (`WORKER_SHUTDOWN_GRACE_SECONDS`) and then cancels cleanly, and the run shows "worker restarted".

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
terraform init -backend-config=backend.hcl # S3 backend, use_lockfile = true (no DynamoDB); aws + random providers
terraform plan -out tfplan                 # needs terraform.tfvars (A3)
terraform apply tfplan                     # ~10-15 min, mostly the RDS instance (5-10 min)
terraform output                           # api_url, vercel_env, cognito_*, rds_endpoint, github_deploy_role_arn, ...
```

Commit the generated `infra/terraform/.terraform.lock.hcl` so every machine uses the same provider builds. If the account already has a GitHub OIDC provider, set `github_oidc_provider_arn`, because an account can only have one.

> **Coming from the earlier Supabase-based setup?** If `put_ssm_params.sh` once wrote `/dcf/prod/DATABASE_URL`, the first apply fails with `ParameterAlreadyExists`. Delete it first (`aws ssm delete-parameter --name /dcf/prod/DATABASE_URL`) or adopt it (`terraform import aws_ssm_parameter.database_url /dcf/prod/DATABASE_URL`; the next apply overwrites the value). Remove the other leftovers with `infra/scripts/put_ssm_params.sh --delete-legacy`. There is no automatic data or user migration: RDS starts empty (the first deploy runs the migrations), and Cognito users are new identities.

What it creates (prefix `dcf`):

| Resource | Details |
|---|---|
| `aws_instance.app` | AL2023 **arm64**, `t4g.small`, a default-VPC subnet in an AZ that offers the type, IMDSv2 only (hop limit 2 so containers can use the role), gp3 30 GB **encrypted** root, auto-recovery on, AMI and subnet changes ignored (no surprise replacement), tags `Name`/`Role=dcf-app` |
| `user_data` (`templates/user_data.sh.tftpl`) | Runs once: Docker, compose plugin (pinned and checksum-verified), 2 GB swapfile, json-file log rotation (10 MB × 5), `live-restore`, `/opt/dcf`, a `dcf-compose` shell alias. App files arrive with each deploy |
| `aws_eip.app` + association | A stable public IPv4, which the sslip.io name and your DNS point at |
| `aws_security_group.app` | Inbound TCP 80/443 and UDP 443 (HTTP/3) from `0.0.0.0/0` and `::/0`. **No SSH.** All egress |
| `aws_db_instance.main` (`dcf-db`) | PostgreSQL `17` (region default minor; auto minor upgrades), `db.t4g.micro`, 20 GB **gp3**, storage autoscaling to 50 GB, **encrypted**, single-AZ in the **same AZ as the app**, `publicly_accessible = false`, backups **7 days** (window 07:00–07:30 UTC), maintenance Sun 08:00–08:30 UTC, `deletion_protection`, final snapshot `dcf-db-final` on delete, Performance Insights / Enhanced Monitoring off. Database `dcf`, master user `dcf_app` |
| `aws_db_parameter_group.main` | `postgres17` family with **`rds.force_ssl = 1`** (non-TLS connections are refused) |
| `aws_db_subnet_group.main`, `aws_security_group.db` | All default-VPC subnets; inbound **5432 only from the `dcf-app` security group**, no egress |
| `random_password.db` → `aws_ssm_parameter.database_url` | 32-char alphanumeric master password; SecureString **`/dcf/prod/DATABASE_URL`** = `postgresql+psycopg://dcf_app:<password>@<rds endpoint>:5432/dcf?sslmode=require`. In Terraform state (see the note under [Architecture](#architecture)) |
| `aws_cognito_user_pool.main` (`dcf-users`) | **Essentials** tier, email as username (case-insensitive, auto-verified), invite-only unless `cognito_allow_self_signup`, password ≥ 12 (upper/lower/digit), first factors `PASSWORD` (+ `EMAIL_OTP`), **optional TOTP MFA**, recovery via verified email, `COGNITO_DEFAULT` email sender, deletion protection |
| `aws_cognito_user_pool_domain.main` | Managed login **v2** on `https://<prefix>.auth.<region>.amazoncognito.com` (`random_id` suffix unless `cognito_domain_prefix`) |
| `aws_cognito_user_pool_client.web` + `aws_cognito_managed_login_branding.web` | **Public** client (no secret): `code` flow (PKCE), scopes `openid email`, IdP `COGNITO`, callbacks `<frontend_origins>/auth/callback` (+ localhost), sign-out `<frontend_origins>/` (+ localhost), access/ID tokens 1 h, refresh 30 d, token revocation on, user-existence errors hidden, auth flows `ALLOW_USER_AUTH` + `ALLOW_REFRESH_TOKEN_AUTH`. Default Cognito branding |
| IAM `dcf-app-instance` role and profile | `AmazonSSMManagedInstanceCore`; ECR pull (2 repos); S3 List/Get/Put/Delete on the bucket; `ssm:GetParameter*` on `/dcf/prod/*`; `kms:Decrypt` only via SSM. (Nothing RDS- or Cognito-specific: the DB is reached over the network with `DATABASE_URL`, and token validation only fetches the public JWKS) |
| GitHub OIDC provider and `dcf-github-deploy` role | Trust: `repo:<owner>/<repo>:ref:refs/heads/main`. Permissions: ECR push to the 2 repos, `s3:PutObject` on `deploy/*`, read `/dcf/prod/config/*` (**not** the secrets or `DATABASE_URL`), `ssm:SendCommand` only on this instance plus `AWS-RunShellScript`, and the read-only `GetCommandInvocation`/`DescribeInstanceInformation` |
| S3 `dcf-app-artifacts-<account-id>` | Public access blocked, SSE-S3, TLS-only policy, BucketOwnerEnforced. Lifecycle: `models/` and `runs/` expire after **35 days**, `deploy/` bundles after 90 days, abort incomplete multipart uploads after 7 days. `edgar-raw/` and `damodaran/` are kept |
| ECR `dcf-api`, `dcf-worker` | Scan on push; lifecycle **keeps the last 10** images |
| SSM `/dcf/prod/config/*` (String) | `APP_DOMAIN`, `PUBLIC_API_BASE_URL`, `CORS_ORIGINS` (= `frontend_origins`), `ACME_EMAIL`, `AWS_REGION`, `S3_BUCKET_NAME`, `ECR_REGISTRY`, `INSTANCE_ID`, `WORKER_CONCURRENCY`, **`COGNITO_REGION`, `COGNITO_USER_POOL_ID`, `COGNITO_APP_CLIENT_ID`** |
| `aws_budgets_budget.monthly` | $45/month, email at 80% and 100% of actual spend and 100% of forecast |
| CloudWatch alarms (`enable_status_check_alarms`) | System status check failure → **recover**; instance status check failure (for example an OOM hang) → **reboot** |

Useful outputs: `vercel_env` (all five `NEXT_PUBLIC_*` values), `cognito_domain_url`, `cognito_client_id`, `cognito_user_pool_id`, `cognito_region`, `cognito_callback_urls`, `rds_endpoint`, `database_url_ssm_parameter`, `db_port_forward_command`, `api_url`, `instance_id`, `github_deploy_role_arn`.

## B2. Secrets into SSM

```bash
infra/scripts/put_ssm_params.sh --dry-run    # validates .env, writes nothing
infra/scripts/put_ssm_params.sh              # uses the same region as Terraform (AWS_REGION or aws configure)
infra/scripts/put_ssm_params.sh --delete-legacy   # also removes old SUPABASE_* / DATABASE_POOLER_URL parameters
```

It writes only the owner-supplied values, under `/dcf/prod/`:

| Parameter | Type | From `.env` |
|---|---|---|
| `ANTHROPIC_API_KEY` | SecureString | |
| `FRED_API_KEY` | SecureString | |
| `SEC_EDGAR_USER_AGENT` | String | |
| `ANTHROPIC_MODEL` | String | default `claude-sonnet-5` |

Everything else the app needs comes from Terraform: `DATABASE_URL` (SecureString) and `COGNITO_REGION`, `COGNITO_USER_POOL_ID`, `COGNITO_APP_CLIENT_ID`, `CORS_ORIGINS`, … (`config/*`). The script **never** writes those names, even when your local `.env` has them.

Safeguards:
- It never prints values, and it passes them through 0600 temp files rather than argv.
- It refuses `.env.example` placeholders and empty values.
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
2. It **renders `/opt/dcf/.env`** (0600) from every parameter under `/dcf/prod`. The Terraform `config/*` values win on a name clash. It adds `IMAGE_TAG=<sha>`. It refuses to continue if a required key is missing: `DATABASE_URL`, `COGNITO_REGION`, `COGNITO_USER_POOL_ID`, `COGNITO_APP_CLIENT_ID`, `ANTHROPIC_API_KEY`, `FRED_API_KEY`, `SEC_EDGAR_USER_AGENT`, and the Terraform config (`APP_DOMAIN`, `PUBLIC_API_BASE_URL`, `ACME_EMAIL`, `AWS_REGION`, `S3_BUCKET_NAME`, `ECR_REGISTRY`). Unrecognized legacy parameters are skipped with a warning.
3. It logs in to ECR, then runs `docker compose pull`.
4. It runs **`alembic upgrade head`** in a one-off container of the **new** api image (`docker compose run --rm --no-deps api …`), against RDS over TLS. **If that fails, it aborts**: the previous `.env` is restored and the running containers are not touched.
5. It runs `docker compose up -d --remove-orphans`, which recreates the containers whose image or env changed. It reloads Caddy if the Caddyfile changed.
6. It waits for the api healthcheck and for all 4 services to be running. It prints logs and fails if they are not ready after 180 s.
7. It probes `https://<api host>/api/health` through Caddy. This is only a warning on the first deploy, while the certificate is being issued.
8. It records `CURRENT_TAG` and `PREVIOUS_TAG` and runs `docker image prune -af`.

Deploys cause a short blip, a few seconds while the api container restarts. Open SSE streams drop, and `EventSource` reconnects on its own. The worker gets SIGTERM with a 90 s `stop_grace_period`: SAQ drains for 60 s (`WORKER_SHUTDOWN_GRACE_SECONDS`), then cancels the remaining jobs cleanly. **Keep migrations backward-compatible** (expand, then contract), because they run while the old containers are still serving.

The first deploy after `terraform apply` can take 5–10 minutes: cloud-init, the first image pull (about 1.5 GB for the worker with LibreOffice), and certificate issuance. If it runs while RDS is still being created, the migration step fails to connect and nothing is started; re-run the deploy once `terraform apply` has finished.

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
dcf-compose run --rm --no-deps -T api alembic current   # migration revision in RDS
cat /opt/dcf/CURRENT_TAG /opt/dcf/PREVIOUS_TAG
free -m; docker stats --no-stream      # memory pressure on t4g.small
sudo SKIP_MIGRATIONS=true /opt/dcf/deploy.sh <sha>   # re-run / roll back from the box (image must be in ECR)
```

| Task | How |
|---|---|
| **Rollback** | `infra/scripts/deploy.sh --skip-build --skip-migrations --tag <older-sha>`, or `/opt/dcf/deploy.sh` on the box. ECR keeps the last 10 images. Migrations are never downgraded automatically. |
| **Change config** (origins, domain, worker concurrency, Cognito options) | Edit `terraform.tfvars`, run `terraform apply`, then redeploy (`deploy.sh --skip-build --tag <current sha>`). |
| **Rotate an API key** | Update `.env`, run `put_ssm_params.sh`, then redeploy. The deploy re-renders `.env`, and compose recreates the api and worker. |
| **Rotate the DB password** | [Below](#rotate-the-database-password). |
| **Backups** | RDS: automated daily snapshots plus transaction logs, **7 days** of point-in-time restore; [restore below](#database-backups-and-restore). Take a manual snapshot before risky migrations. Artifacts in S3 are regenerable caches. The only local state on the box is the `caddy_data` volume (certificates), which is re-issued automatically if lost; avoid destroying it repeatedly, because of Let's Encrypt rate limits. |
| **OS patches** | Monthly, from a session: `sudo dnf upgrade --releasever=latest -y && sudo reboot`. Docker and every container (`restart: unless-stopped`) come back on their own. RDS minor versions are patched automatically in the Sunday maintenance window. |
| **DB connections** | Each app process opens at most 10 connections (SQLAlchemy pool 5 + 5 overflow): 2 uvicorn workers + 1 SAQ worker = **≤ 30**, plus one short-lived Alembic container per deploy. `db.t4g.micro` allows roughly 80–110 (`max_connections` scales with RAM; `show max_connections;`), so there is ample headroom for a tunnelled `psql`. Raise `worker_concurrency` or uvicorn workers with this budget in mind. |
| **Disk** | `docker system df`. Each deploy prunes unused images. The root volume is 30 GB. RDS storage grows automatically up to 50 GB (`db_max_allocated_storage_gb`); it never shrinks. |
| **Host failure** | EC2 auto-recovery, plus the CloudWatch alarms that recover or reboot the instance. The EIP, volume and instance id stay the same. RDS is single-AZ: an AZ outage takes the DB down until AWS recovers it (or you restore to another AZ). |
| **Pause to save money** | `aws ec2 stop-instances --instance-ids <id>` stops compute billing, and `aws rds stop-db-instance --db-instance-identifier dcf-db` stops DB compute (RDS restarts it automatically after 7 days). The EIP (about $3.65/month) and all storage still bill. |

### Connect to the database

RDS has no public address. Tunnel through the app instance with Session Manager (the instance role already allows it; the DB security group admits the instance):

```bash
cd infra/terraform
# terminal 1: forward localhost:15432 -> RDS:5432 (same as `terraform output -raw db_port_forward_command`)
aws ssm start-session --target "$(terraform output -raw instance_id)" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "host=$(terraform output -raw rds_endpoint),portNumber=5432,localPortNumber=15432"

# terminal 2: psql with the production credentials, pointed at the tunnel (TLS is still required)
DBURL="$(aws ssm get-parameter --name /dcf/prod/DATABASE_URL --with-decryption --query Parameter.Value --output text)"
psql "$(printf %s "$DBURL" | sed -E 's#^postgresql\+psycopg://#postgresql://#; s#@[^/]+/#@localhost:15432/#')"
```

This is the master user: be careful. The same tunnel works for `pg_dump` / `pg_restore` and GUI clients (host `localhost`, port `15432`, SSL mode `require`). Reading `/dcf/prod/DATABASE_URL` needs `ssm:GetParameter` + `kms:Decrypt` (admins); the GitHub deploy role cannot.

### Database backups and restore

- **Automated backups**: RDS keeps daily snapshots and transaction logs for 7 days (`db_backup_retention_days`, up to 35). Backup storage up to 100% of the provisioned storage (20 GB) is free.
- **Manual snapshot** (kept until you delete it, $0.095/GB-month beyond the free allowance): `aws rds create-db-snapshot --db-instance-identifier dcf-db --db-snapshot-identifier dcf-db-before-<change>`.
- **Restore** always creates a **new** instance. The simplest procedure, for this small database:
  1. Restore next to production:
     ```bash
     # --restore-time <UTC timestamp>, or --use-latest-restorable-time
     aws rds restore-db-instance-to-point-in-time \
       --source-db-instance-identifier dcf-db --target-db-instance-identifier dcf-db-restore \
       --restore-time 2026-01-31T12:00:00Z \
       --db-instance-class db.t4g.micro --no-multi-az --no-publicly-accessible \
       --db-subnet-group-name dcf-db --vpc-security-group-ids <dcf-db security group id> \
       --db-parameter-group-name <dcf-pg17-… parameter group name>
     # from a snapshot instead: aws rds restore-db-instance-from-db-snapshot --db-snapshot-identifier <snap> ... (same options)
     ```
  2. Tunnel to `dcf-db-restore` (as above, with its endpoint and another local port), then copy what you need into production, for example the whole database: stop the api and worker (`dcf-compose stop api worker`), `pg_dump -Fc` from the restore, `pg_restore --clean --if-exists --no-owner -d dcf` into production, start them again.
  3. Delete the restored instance: `aws rds delete-db-instance --db-instance-identifier dcf-db-restore --skip-final-snapshot`.

  Replacing `dcf-db` wholesale with the restored instance (rename + `terraform import`) also works but needs Terraform state surgery; use the copy approach unless the database has grown large.

### Rotate the database password

```bash
cd infra/terraform
terraform apply -replace=random_password.db   # new password -> RDS (applied immediately) + /dcf/prod/DATABASE_URL
cd ../..
infra/scripts/deploy.sh --skip-build --skip-migrations --tag <current sha>   # or re-run the latest deploy workflow
```

`<current sha>` is in `/opt/dcf/CURRENT_TAG` on the box (or the last green **deploy** run). Redeploy right after the apply: open connections keep working, but new connections from the old containers fail until they are recreated with the new `DATABASE_URL`. Expect a minute of errors at most. The new password is again in Terraform state.

### Upgrade PostgreSQL (major version)

Minor versions upgrade automatically. For a major version (for example 17 → 18): take a manual snapshot, try the upgrade on a restored copy first, then set `db_engine_version = "18"` and `terraform apply`. A new `postgres18` parameter group is created first; the upgrade itself happens in the next maintenance window (`apply_immediately = false`), or immediately with `aws rds modify-db-instance --db-instance-identifier dcf-db --apply-immediately …`. Expect 10–20 minutes of downtime.

### Cognito user administration

`POOL="$(terraform -chdir=infra/terraform output -raw cognito_user_pool_id)"`, then:

| Task | Command (console: **Cognito → User pools → dcf-users → Users → <user>**) |
|---|---|
| List users | `aws cognito-idp list-users --user-pool-id "$POOL" --query 'Users[].[Username,UserStatus,Enabled,Attributes[?Name==\`email\`].Value\|[0]]' --output table` |
| Invite / resend invite | [A6](#a6-users-cognito) |
| Disable / enable | `aws cognito-idp admin-disable-user --user-pool-id "$POOL" --username alice@example.com` (`admin-enable-user`) |
| Force sign-out everywhere | `aws cognito-idp admin-user-global-sign-out --user-pool-id "$POOL" --username alice@example.com` (revokes refresh tokens; access tokens stay valid until they expire, ≤ 1 h) |
| Reset password | `aws cognito-idp admin-reset-user-password --user-pool-id "$POOL" --username alice@example.com` (emails a code) |
| Remove a lost authenticator app | `aws cognito-idp admin-set-user-mfa-preference --user-pool-id "$POOL" --username alice@example.com --software-token-mfa-settings Enabled=false` |
| Delete | `aws cognito-idp admin-delete-user --user-pool-id "$POOL" --username alice@example.com` (the user's `users` row and runs in Postgres are not deleted; deleting the `users` row cascades to their runs) |

The API sees the user's Cognito `sub` (a UUID) in the access token. Access tokens do not carry the email address.

### Upgrade the instance

Signs that you need to upgrade: builds get OOM-killed (`dmesg | grep -i oom`), swap stays heavily used, or the instance-status alarm reboots the box.

1. In `terraform.tfvars`, set `instance_type = "t4g.medium"` and `worker_concurrency = 4`, and raise `monthly_budget_usd` to about 60.
2. Run `terraform apply`. It stops and starts the instance in place, with about 2 minutes of downtime. The EIP, disk and ID stay the same.
3. Redeploy, to pick up `WORKER_CONCURRENCY`.

The database scales the same way: `db_instance_class = "db.t4g.small"` (≈ +$12/month) is applied in the next maintenance window.

### Teardown

1. Set `force_destroy = true`, `db_deletion_protection = false` and `cognito_deletion_protection = false`, and run `terraform apply`. This allows deleting a non-empty bucket, ECR repos that hold images, the database and the user pool (**all users are lost**).
2. Run `terraform destroy`. RDS takes the final snapshot `dcf-db-final` first; it is kept (and billed) until you delete it with `aws rds delete-db-snapshot --db-snapshot-identifier dcf-db-final`. Delete or rename it before creating the stack again, because the name is fixed.
3. Delete the owner-supplied parameters, which are not in Terraform state: `aws ssm delete-parameters --names $(aws ssm get-parameters-by-path --path /dcf/prod --query 'Parameters[].Name' --output text)`.
4. The state bucket has `prevent_destroy`. To remove it, empty all its versions in the console, delete the bucket, and delete `infra/terraform/bootstrap/terraform.tfstate`.
5. Delete the Vercel project separately.

---

# Monthly cost

This estimate is for on-demand pricing in us-east-1, running 730 hours a month, with low traffic. Check current prices before you commit.

| Item | Size | ≈ $/month |
|---|---|---|
| EC2 `t4g.small` | 2 vCPU burstable, 2 GB, $0.0168/h | 12.26 |
| EBS gp3 root | 30 GB × $0.08 | 2.40 |
| Public IPv4 (Elastic IP, attached) | $0.005/h | 3.65 |
| **RDS `db.t4g.micro`** PostgreSQL, single-AZ | $0.016/h | **11.68** |
| **RDS gp3 storage** | 20 GB × $0.115 | **2.30** |
| RDS backup storage | up to 100% of provisioned storage (20 GB) is free; 7-day retention of a small DB stays within it | ~0 |
| Cognito user pool, **Essentials** tier | AWS lists a free tier of 10,000 MAU/month for Essentials (then about $0.015 per MAU) at the time of writing; confirm on the [Cognito pricing page](https://aws.amazon.com/cognito/pricing/) | 0 for 20–50 users |
| Cognito email (`COGNITO_DEFAULT`) | 50 emails/day | 0 |
| S3 | a few hundred MB plus requests | ~0.10 |
| ECR | 2 repos × up to 10 images, shared layers (~2 GB) | ~0.20 |
| Data transfer out | first 100 GB/month free; app ↔ DB traffic stays in one AZ (free) | ~0 |
| CloudWatch alarms | 2 standard alarms (10 free) | 0–0.20 |
| SSM Parameter Store (standard), Run Command, Session Manager, Budgets, OIDC | | 0 |
| Terraform state bucket | | ~0.01 |
| **Total AWS** | | **≈ $33–35** |
| `t4g.medium` instead (4 GB, $0.0336/h) | | +≈ $12 |
| `db.t4g.small` instead (2 GB) | | +≈ $12 |
| Burst surplus (`cpu_credits = "unlimited"` on EC2; RDS T4g instances run in unlimited mode) | only if average CPU stays above the baseline | usually 0 |
| Vercel Hobby (personal, non-commercial) · FRED / EDGAR / Damodaran | | 0 |
| Anthropic | per token | usage-based; set a spend limit |

For comparison, the previous ECS Fargate + ALB design cost about $70–100/month.

### Cheaper alternative: Postgres on the box

Not implemented, documented for when $14/month matters more than managed backups. Add a `postgres:17-alpine` service to `docker-compose.prod.yml` with a named volume (on the 30 GB root disk, or a separate gp3 volume), `DATABASE_URL=postgresql+psycopg://…@postgres:5432/dcf`, and drop `database.tf`. You then own: a nightly `pg_dump` to S3 (cron or a compose sidecar) and restore drills, major-version upgrades, disk sizing, and the RAM it takes from the worker on a 2 GB `t4g.small` (plan on `t4g.medium`, which erases most of the savings). Losing the instance or its volume without a recent dump loses data.

---

# Local development

### Option A: docker compose

```bash
cp .env.example .env     # dev values
docker compose up --build   # db (postgres:16) :5432, redis :6379, api :8000, worker, frontend :3000
docker compose run --rm api alembic upgrade head   # once, and after pulling new migrations
```

Compose runs a throwaway Postgres (`db`) and overrides `DATABASE_URL` and `REDIS_URL` for the api and worker to point at the `db` and `redis` containers. The frontend image is built with `NEXT_PUBLIC_API_BASE_URL=http://localhost:8000` and the `NEXT_PUBLIC_COGNITO_*` build args from `.env`. If `NEXT_PUBLIC_COGNITO_CLIENT_ID` is empty, the frontend uses the dev auth bypass (set `DEV_AUTH_BYPASS=true` for the API in `.env`).

To try the production compose file locally, run `docker compose -f infra/deploy/docker-compose.prod.yml config` with a filled-in `.env`. It pulls from ECR and needs a public hostname for the certificate, so it is not meant for laptops.

### Option B: run the processes directly (fastest edit loop)

```bash
# Postgres 16+ locally (RDS runs 17; the app uses nothing version-specific)
PG=/usr/lib/postgresql/16/bin            # or: brew install postgresql@17
$PG/initdb -D ~/.dcf-pg -U postgres --auth=trust && $PG/pg_ctl -D ~/.dcf-pg -l ~/.dcf-pg/log start
#   DATABASE_URL=postgresql+psycopg://postgres@localhost:5432/postgres  in .env
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

Auth, two options:
- **No Cognito (default for local work):** set `DEV_AUTH_BYPASS=true` on the API and leave `NEXT_PUBLIC_COGNITO_CLIENT_ID` unset in the frontend. The frontend sends `Bearer dev-bypass-token` and the API maps it to a fixed dev user, which it upserts into `users`. **Never** enable this in production; `docker-compose.prod.yml` pins `DEV_AUTH_BYPASS=false`.
- **Real Cognito:** with `cognito_localhost_callbacks = true` (the default), the production pool already accepts `http://localhost:3000/auth/callback` and `http://localhost:3000/`. Put the `terraform output vercel_env` values (with `NEXT_PUBLIC_API_BASE_URL=http://localhost:8000`) in `frontend/.env.local`, and `COGNITO_REGION`, `COGNITO_USER_POOL_ID`, `COGNITO_APP_CLIENT_ID` (same values) in the API's `.env`, with `DEV_AUTH_BYPASS=false`. You sign in as a real production user, against your local database. For a separate dev pool, apply this Terraform configuration into another account or with another `project` name and state key.

Other notes:
- `STORAGE_BACKEND=local`, with `LOCAL_STORAGE_DIR` and `PUBLIC_API_BASE_URL`, writes artifacts to disk instead of S3. Download links become signed, expiring `GET /api/files/...` URLs on the API. Production pins `s3`.
- Without `ANTHROPIC_API_KEY`, proposals and report prose use the deterministic fallback. Without `FRED_API_KEY`, set `RISK_FREE_RATE_OVERRIDE=0.042`, and the run is flagged. A minimal offline-ish backend `.env` sets `DATABASE_URL=postgresql+psycopg://postgres@localhost:5432/postgres`, `REDIS_URL=redis://localhost:6379`, `STORAGE_BACKEND=local`, `DEV_AUTH_BYPASS=true` and `RISK_FREE_RATE_OVERRIDE=0.042`. EDGAR and Yahoo Finance still need internet access.
- Do not point a laptop at the production RDS instance except through the [SSM tunnel](#connect-to-the-database), and never run tests against it.
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
