"""
Standardized error responses for frappe_payments.

Extends frappe_auth's error code ranges with payment-specific codes (7000-7999).
Re-exports auth error helpers so callers only need one import.
"""

import frappe
from frappe import _

# ---------------------------------------------------------------------------
# Re-export frappe_auth helpers so callers can do:
#   from frappe_payments.utils.error_handler import throw_error, success_response
# ---------------------------------------------------------------------------
from frappe_auth.utils.error_handler import (  # noqa: F401
    ErrorCode as _BaseErrorCode,
    throw_error,
    success_response,
    error_response,
)


class ErrorCode(_BaseErrorCode):
    """Payment-gateway error codes (7000-7999)."""

    # Gateway configuration
    GATEWAY_NOT_CONFIGURED = 7001
    GATEWAY_DISABLED = 7002

    # Transaction errors
    PAYMENT_DECLINED = 7101
    PAYMENT_ERROR = 7102
    INVALID_PAYMENT_TOKEN = 7103
    DUPLICATE_TRANSACTION = 7104

    # Post-payment document errors
    INVOICE_CREATION_FAILED = 7201
    PAYMENT_ENTRY_FAILED = 7202


_PAYMENT_MESSAGES = {
    ErrorCode.GATEWAY_NOT_CONFIGURED: _("Payment gateway is not configured"),
    ErrorCode.GATEWAY_DISABLED: _("Payment gateway is currently disabled"),
    ErrorCode.PAYMENT_DECLINED: _("Payment was declined"),
    ErrorCode.PAYMENT_ERROR: _("A payment processing error occurred"),
    ErrorCode.INVALID_PAYMENT_TOKEN: _("Invalid or expired payment token"),
    ErrorCode.DUPLICATE_TRANSACTION: _("Duplicate transaction detected"),
    ErrorCode.INVOICE_CREATION_FAILED: _("Failed to create invoice after payment"),
    ErrorCode.PAYMENT_ENTRY_FAILED: _("Failed to record payment entry after charge"),
}


def throw_payment_error(error_code, message=None, http_status_code=None, **kwargs):
    """
    Raise a payment-specific error with a standardised structure.

    Falls back to throw_error for non-payment codes.
    """
    if error_code not in _PAYMENT_MESSAGES:
        throw_error(error_code, message=message, http_status_code=http_status_code, **kwargs)
        return

    error_message = message or _PAYMENT_MESSAGES[error_code]

    if http_status_code:
        frappe.local.response["http_status_code"] = http_status_code

    error_data = {"error_code": error_code, "message": error_message, **kwargs}
    frappe.local.response["error_data"] = error_data
    frappe.local.response.pop("exc", None)

    frappe.throw(error_message, frappe.ValidationError)
