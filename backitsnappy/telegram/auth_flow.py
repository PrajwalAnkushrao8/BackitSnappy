"""First-run auth state machine, shared by client_manager and the setup API routes."""
from enum import Enum


class AuthState(str, Enum):
    NEEDS_PHONE = "needs_phone"              # first screen: not signed in, no phone entered yet
    NEEDS_CREDENTIALS = "needs_credentials"  # phone entered, but it has no api_id/api_hash bound yet
    NEEDS_CODE = "needs_code"                # login code requested, awaiting entry
    NEEDS_PASSWORD = "needs_password"        # 2FA password required
    AUTHORIZED = "authorized"
