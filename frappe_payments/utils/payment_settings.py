"""
NMI Settings cache — mirrors the pattern in frappe_auth.utils.auth_settings.

Security note: the API key (a secret) is never stored in Redis.
Only non-sensitive configuration is cached. The key is always read
fresh from the encrypted `__Auth` table via get_decrypted_password().
"""

import frappe

_CACHE_KEY = "frappe_payments_nmi_settings"
_CACHE_TTL = 300  # 5 minutes


def get_nmi_settings() -> dict:
    """
    Return NMI Settings.

    Non-sensitive config is cached in Redis (5 min TTL).
    The API key is always fetched fresh — never stored in Redis.

    Returns:
        {
            "enabled": bool,
            "environment": "Sandbox" | "Production",
            "api_key": str,
            "tokenization_key": str,
            "base_url": str,
        }
    """
    cached = frappe.cache().get_value(_CACHE_KEY)

    if not cached:
        doc = frappe.get_doc("NMI Settings", "NMI Settings")
        cached = {
            "enabled": bool(doc.enabled),
            "environment": doc.environment or "Sandbox",
            "tokenization_key": doc.tokenization_key or "",
            "base_url": _resolve_base_url(doc.environment),
        }
        frappe.cache().set_value(_CACHE_KEY, cached, expires_in_sec=_CACHE_TTL)

    return {**cached, "api_key": _get_api_key()}


def _get_api_key() -> str:
    """
    Reliably decrypt and return the NMI API key.

    Frappe Password fields are stored encrypted in the `__Auth` table and are
    NOT populated on doc.field_name after a normal get_doc() call.
    get_decrypted_password() is the correct way to read them.
    """
    from frappe.utils.password import get_decrypted_password

    try:
        key = get_decrypted_password(
            "NMI Settings", "NMI Settings", "api_key", raise_exception=False
        )
        if key:
            return key
    except Exception:
        pass

    # Fallback: Document.get_password() wraps the same logic
    try:
        return frappe.get_doc("NMI Settings", "NMI Settings").get_password("api_key") or ""
    except Exception:
        pass

    frappe.log_error(
        title="NMI Settings - API Key Unreadable",
        message="Could not decrypt the NMI API key. "
                "Re-save NMI Settings in the Desk to re-encrypt it.",
    )
    return ""


def invalidate_settings_cache(doc=None, method=None):
    """Called from NMISettings.on_update and hooks doc_events."""
    frappe.cache().delete_value(_CACHE_KEY)


def _resolve_base_url(environment: str) -> str:
    if environment == "Production":
        return "https://secure.nmi.com/api/v5"
    return "https://sandbox.nmi.com/api/v5"
