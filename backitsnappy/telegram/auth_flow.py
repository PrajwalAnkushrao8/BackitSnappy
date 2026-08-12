"""First-run auth state machine, shared by client_manager and the setup API routes."""
from enum import Enum


class AuthState(str, Enum):
    NEEDS_CREDENTIALS = "needs_credentials"  # no api_id/api_hash saved yet
    NEEDS_PHONE = "needs_phone"              # have credentials, not signed in
    NEEDS_CODE = "needs_code"                # login code requested, awaiting entry
    NEEDS_PASSWORD = "needs_password"        # 2FA password required
    AUTHORIZED = "authorized"
