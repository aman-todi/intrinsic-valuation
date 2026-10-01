# ============================================================================
# Amazon Cognito: user pool, managed login (v2) domain + branding, and the frontend's public app client.
#
# The Next.js frontend signs users in with the Authorization Code flow + PKCE against the managed login
# pages (no client secret; the browser is a public client). The API validates Cognito ACCESS tokens:
# issuer https://cognito-idp.<region>.amazonaws.com/<pool id>, token_use = "access",
# client_id = the app client id below (access tokens carry client_id, not aud).
#
# Email goes through Cognito's built-in sender (COGNITO_DEFAULT), which is limited to 50 emails/day
# per account (invites, verification codes, password resets and email OTP sign-in codes all count).
# Upgrade path: a verified SES identity + email_configuration { email_sending_account = "DEVELOPER" }.
# ============================================================================

locals {
  # Cognito accepts http:// only for localhost; everything else must be https.
  cognito_callback_urls = concat(
    [for o in var.frontend_origins : "${o}/auth/callback"],
    var.cognito_localhost_callbacks ? ["http://localhost:3000/auth/callback"] : [],
  )
  cognito_logout_urls = concat(
    [for o in var.frontend_origins : "${o}/"],
    var.cognito_localhost_callbacks ? ["http://localhost:3000/"] : [],
  )

  cognito_first_auth_factors = var.cognito_email_otp_enabled ? ["PASSWORD", "EMAIL_OTP"] : ["PASSWORD"]
  cognito_domain_prefix      = var.cognito_domain_prefix != "" ? var.cognito_domain_prefix : "${var.project}-${random_id.cognito_domain.hex}"
  cognito_domain_url         = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${local.region}.amazoncognito.com"
  # Invite emails link to the app when it exists (first frontend origin), else just name it.
  app_url_for_invites = length(var.frontend_origins) > 0 ? var.frontend_origins[0] : "the DCF valuation app"
}

# Domain prefixes are global per region; a random suffix avoids collisions. Never regenerated.
resource "random_id" "cognito_domain" {
  byte_length = 4
}

resource "aws_cognito_user_pool" "main" {
  name = "${local.name}-users"

  # Essentials: managed login v2 and choice-based sign-in (email OTP). Free tier: see docs/DEPLOYMENT.md.
  user_pool_tier      = "ESSENTIALS"
  deletion_protection = var.cognito_deletion_protection ? "ACTIVE" : "INACTIVE"

  # The email address is the username (no separate username). Case-insensitive.
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  username_configuration {
    case_sensitive = false
  }

  # A changed email stays unverified (and the old one keeps working) until the new one is confirmed.
  user_attribute_update_settings {
    attributes_require_verification_before_update = ["email"]
  }

  sign_in_policy {
    allowed_first_auth_factors = local.cognito_first_auth_factors
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 7
  }

  # Optional TOTP (authenticator app). Note: a user who turns on TOTP can no longer use the email-OTP
  # first factor (Cognito does not combine MFA with passwordless for the same user); password still works.
  mfa_configuration = "OPTIONAL"
  software_token_mfa_configuration {
    enabled = true
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  email_configuration {
    email_sending_account = "COGNITO_DEFAULT"
  }

  # Open self sign-up by default (email verified by code). cognito_allow_self_signup = false makes the pool
  # invite-only: users are created by an admin (admin-create-user or the console) with a temporary password.
  admin_create_user_config {
    allow_admin_create_user_only = var.cognito_allow_self_signup ? false : true

    invite_message_template {
      email_subject = "Your DCF valuation app account"
      email_message = "You have been invited to ${local.app_url_for_invites}. Sign in with {username} and the temporary password {####} (valid for 7 days); you will be asked to choose a new one."
      sms_message   = "DCF app: username {username}, temporary password {####}"
    }
  }

  verification_message_template {
    default_email_option = "CONFIRM_WITH_CODE"
  }

  tags = { Name = "${local.name}-users" }
}

# Managed login (v2) on the Cognito-hosted prefix domain:
# https://<prefix>.auth.<region>.amazoncognito.com (NEXT_PUBLIC_COGNITO_DOMAIN).
resource "aws_cognito_user_pool_domain" "main" {
  domain                = local.cognito_domain_prefix
  user_pool_id          = aws_cognito_user_pool.main.id
  managed_login_version = 2
}

# Public client for the Next.js app: Authorization Code + PKCE, no secret.
resource "aws_cognito_user_pool_client" "web" {
  name         = "${local.name}-web"
  user_pool_id = aws_cognito_user_pool.main.id

  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = local.cognito_callback_urls
  logout_urls                          = local.cognito_logout_urls

  # ALLOW_USER_AUTH = choice-based sign-in (password or email OTP) used by managed login.
  explicit_auth_flows = ["ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_AUTH"]

  access_token_validity  = 1
  id_token_validity      = 1
  refresh_token_validity = 30
  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }

  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true

  lifecycle {
    precondition {
      condition     = length(local.cognito_callback_urls) > 0
      error_message = "Cognito needs at least one callback URL: set frontend_origins or cognito_localhost_callbacks = true."
    }
  }
}

# Managed login v2 needs a branding style per app client; use Cognito's default look.
resource "aws_cognito_managed_login_branding" "web" {
  user_pool_id                = aws_cognito_user_pool.main.id
  client_id                   = aws_cognito_user_pool_client.web.id
  use_cognito_provided_values = true
}
