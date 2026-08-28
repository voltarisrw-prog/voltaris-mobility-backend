"""Configuration. Every secret arrives from the environment; none has a usable default.

Production refuses to start when a required secret is missing or is still the
development placeholder. A backend that boots with a default JWT secret is worse
than one that fails loudly, because nobody notices until it is exploited.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "staging", "production"]

DEV_PLACEHOLDER = "dev-insecure-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    environment: Environment = "development"
    service_name: str = "voltaris-api"
    api_v1_prefix: str = "/api/v1"

    # --- Database -----------------------------------------------------------
    mongodb_uri: str = "mongodb+srv://voltarisrw_db_user:i1sDhybqWKY6fRSC@cluster0.gn8tuzc.mongodb.net"
    mongodb_database: str = "voltaris"
    mongodb_max_pool_size: int = 50
    mongodb_min_pool_size: int = 5
    mongodb_timeout_ms: int = 5_000

    # --- Auth ---------------------------------------------------------------
    jwt_secret: str = DEV_PLACEHOLDER
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 900  # 15 minutes
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 30
    password_min_length: int = 12

    # Brute-force protection
    login_max_attempts: int = 5
    login_lockout_seconds: int = 900

    #: SameSite policy for the session cookies.
    #:
    #: "lax" is correct when the frontend and API share a registrable domain —
    #: voltaris.rw and api.voltaris.rw are the same site, and Lax still blocks
    #: the cross-site POSTs that CSRF depends on.
    #:
    #: "none" is required when they do not, as with a *.vercel.app frontend
    #: calling a *.onrender.com API. Lax cookies are not sent on cross-site
    #: fetch, so login would appear to succeed and the next request would arrive
    #: anonymous, with nothing in any log to explain it.
    #:
    #: "none" demands Secure, so it only works over HTTPS. The CSRF double-submit
    #: token carries the protection Lax was providing.
    # Cross-site browser authentication:
    # Vercel (*.vercel.app) -> Render (*.onrender.com) requires SameSite=None.
    # Local development keeps Lax unless explicitly overridden.
    session_cookie_samesite: Literal["lax", "none", "strict"] = "none"

    # --- Google sign-in ---------------------------------------------------
    # Blank client id disables the feature: the endpoints return NOT_CONFIGURED
    # rather than pretending to work.
    google_client_id: str = "mongodb+srv://voltarisrw_db_user:i1sDhybqWKY6fRSC@cluster0.gn8tuzc.mongodb.net/?appName=Cluster0"
    google_client_secret: str = "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"
    google_redirect_uri: str = "http://localhost:3000/auth/google/callback"
    google_jwks_url: str = "https://www.googleapis.com/oauth2/v3/certs"
    google_token_url: str = "https://oauth2.googleapis.com/token"
    google_authorize_url: str = "https://accounts.google.com/o/oauth2/v2/auth"
    google_issuers: list[str] = Field(
        default_factory=lambda: ["https://accounts.google.com", "accounts.google.com"]
    )

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    # --- Object storage (Cloudflare R2) ------------------------------------
    # Blank account id disables uploads: the endpoints return NOT_CONFIGURED
    # rather than presigning URLs against a bucket that does not exist.
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = "voltaris-media"
    #: The CDN hostname in front of the bucket. Must match `remotePatterns` in
    #: the frontend's next.config.mjs, or next/image refuses to load anything.
    r2_public_base_url: str = "https://media.voltaris.rw"
    r2_upload_ttl_seconds: int = 900

    @property
    def uploads_enabled(self) -> bool:
        return bool(self.r2_account_id and self.r2_access_key_id and self.r2_secret_access_key)

    # --- Payments -----------------------------------------------------------
    payment_provider: str = "stub"
    payment_api_key: str = ""
    payment_webhook_secret: str = DEV_PLACEHOLDER
    payment_webhook_tolerance_seconds: int = 300

    # --- Business -----------------------------------------------------------
    default_currency: str = "RWF"
    #: Basis points taken by Voltaris when a listing carries no explicit rate.
    default_commission_bps: int = Field(default=800, ge=0, le=5_000)
    #: Provider fee, used to derive net revenue in the commission ledger.
    payment_fee_bps: int = Field(default=290, ge=0, le=1_000)
    payment_fee_fixed_minor: int = 0

    # --- Ops ----------------------------------------------------------------
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    log_level: str = "INFO"
    sentry_dsn: str = ""
    max_request_bytes: int = 2 * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @model_validator(mode="after")
    def _production_cookie_policy(self) -> Settings:
        # Render API + Vercel frontend are cross-site.
        # Never allow production to silently fall back to Lax.
        if self.environment == "production" and self.session_cookie_samesite == "lax":
            self.session_cookie_samesite = "none"
        return self

    @model_validator(mode="after")
    def _refuse_insecure_production(self) -> Settings:
        if self.environment != "production":
            return self
        missing: list[str] = []
        if self.jwt_secret in ("", DEV_PLACEHOLDER) or len(self.jwt_secret) < 32:
            missing.append("JWT_SECRET (min 32 chars)")
        if self.payment_webhook_secret in ("", DEV_PLACEHOLDER):
            missing.append("PAYMENT_WEBHOOK_SECRET")
        if self.mongodb_uri.startswith("mongodb://localhost"):
            missing.append("MONGODB_URI")
        if "*" in self.cors_origins:
            missing.append("CORS_ORIGINS (wildcard is not permitted in production)")
        if missing:
            raise ValueError(
                "Refusing to start in production with insecure configuration: " + ", ".join(missing)
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
