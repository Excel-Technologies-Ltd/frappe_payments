"""
HTTP client for the NMI Payments REST API v5.

Docs:    https://docs.nmi.com/docs/integration-guide-implement-backend
         https://docs.nmi.com/reference/create-sale-v5

Endpoint: POST https://sandbox.nmi.com/api/v5/payments/sale  (sandbox)
          POST https://secure.nmi.com/api/v5/payments/sale   (production)

Auth:     Authorization: <private_api_key>   (bare key, no "Bearer" prefix)
Format:   JSON request / JSON response

Response approval indicator: response == "1"  (classic NMI style inside v5 JSON)
Transaction ID field:        id
"""

import frappe
import requests
from frappe import _

from frappe_payments.utils.error_handler import ErrorCode, throw_payment_error
from frappe_payments.utils.payment_settings import get_nmi_settings

_REQUEST_TIMEOUT = 30  # seconds


def charge(
    *,
    amount: float,
    payment_token: str,
    billing: dict = None,
    order_id: str = None,
    description: str = None,
    customer_email: str = None,
) -> dict:
    """
    Create a Sale transaction (authorize + capture) via NMI REST API v5.

    Args:
        amount:          Charge amount (e.g. 99.99).
        payment_token:   One-time token from Collect.js on the frontend.
        billing:         Optional billing info:
                           {first_name, last_name, address1, city, state,
                            postal, country, phone, email}
        order_id:        Reference ID shown in the NMI dashboard.
        description:     Order description.
        customer_email:  Customer email for the receipt.

    Returns:
        {
            "transaction_id": str,
            "status": str,          # "approved" | "declined" | "error"
            "response_code": str,
            "response_text": str,
            "amount": float,
        }

    Raises frappe.ValidationError on any gateway or transport error.
    """
    settings = _get_active_settings()

    payload = _build_payload(
        amount=amount,
        payment_token=payment_token,
        billing=billing,
        order_id=order_id,
        description=description,
        customer_email=customer_email,
    )

    data = _post(settings["base_url"], settings["api_key"], payload)
    return _parse_response(data)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_active_settings() -> dict:
    settings = get_nmi_settings()

    if not settings.get("enabled"):
        throw_payment_error(ErrorCode.GATEWAY_DISABLED, http_status_code=503)

    if not settings.get("api_key"):
        frappe.log_error(
            title="NMI Settings - API Key Missing",
            message="API key is empty. Open NMI Settings and re-save.",
        )
        throw_payment_error(ErrorCode.GATEWAY_NOT_CONFIGURED, http_status_code=503)

    return settings


def _build_payload(
    *,
    amount: float,
    payment_token: str,
    billing: dict,
    order_id: str,
    description: str,
    customer_email: str,
) -> dict:
    """
    Build the JSON payload for the NMI REST v5 sale endpoint.

    Minimal required payload (from NMI docs):
        {
            "amount": 99.99,
            "payment_details": {
                "payment_token": "<collect_js_token>"
            }
        }

    All optional blocks are only added when they contain non-empty values.
    NMI rejects requests that include fields with empty string values in
    the billing_address object.
    """
    payload = {
        "amount": float(f"{float(amount):.2f}"),
        "payment_details": {
            "payment_token": payment_token,
        },
    }

    # Build billing block — strip all empty/None values before including.
    # NMI's v5 API returns "The provided data is invalid" when empty-string
    # fields (state, postal, etc.) are present in billing_address.
    billing_block = {}

    if billing:
        raw_billing = {
            "first_name": billing.get("first_name") or "",
            "last_name":  billing.get("last_name") or "",
            "address1":   billing.get("address1") or "",
            "city":       billing.get("city") or "",
            "state":      billing.get("state") or "",
            "zip":        billing.get("postal") or "",  # NMI uses "zip"
            "country":    billing.get("country") or "US",
            "phone":      billing.get("phone") or "",
            "email":      billing.get("email") or customer_email or "",
        }
        # Include only fields that have actual values
        billing_block = {k: v for k, v in raw_billing.items() if v}

    elif customer_email:
        billing_block = {"email": customer_email}

    if billing_block:
        payload["billing_address"] = billing_block

    if order_id or description:
        order_block = {}
        if order_id:
            order_block["order_id"] = order_id
        if description:
            order_block["description"] = description
        payload["order_details"] = order_block

    return payload


def _post(base_url: str, api_key: str, payload: dict) -> dict:
    """
    POST JSON to the NMI v5 sale endpoint.
    Returns the parsed JSON response dict.
    """
    url = f"{base_url}/payments/sale"
    headers = {
        "Authorization": api_key,     # bare key — confirmed by NMI docs
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=_REQUEST_TIMEOUT)
    except requests.Timeout:
        frappe.log_error(
            title="NMI Gateway Timeout",
            message=f"POST {url} timed out after {_REQUEST_TIMEOUT}s",
        )
        throw_payment_error(
            ErrorCode.PAYMENT_ERROR,
            message=_("Payment gateway timed out. Please try again."),
            http_status_code=504,
        )
    except requests.RequestException as exc:
        frappe.log_error(title="NMI Gateway Connection Error", message=str(exc))
        throw_payment_error(
            ErrorCode.PAYMENT_ERROR,
            message=_("Could not reach the payment gateway. Please try again."),
            http_status_code=502,
        )

    # Always log the raw NMI response for auditability
    frappe.log_error(
        title="NMI Raw Response",
        message=f"HTTP {resp.status_code}\n{resp.text[:3000]}",
    )

    if resp.status_code == 401:
        throw_payment_error(
            ErrorCode.GATEWAY_NOT_CONFIGURED,
            message=_("NMI rejected the API key. Re-save your NMI Settings."),
            http_status_code=503,
        )

    try:
        return resp.json()
    except ValueError:
        frappe.log_error(
            title="NMI Non-JSON Response",
            message=f"HTTP {resp.status_code}: {resp.text[:500]}",
        )
        throw_payment_error(
            ErrorCode.PAYMENT_ERROR,
            message=_("The payment gateway returned an unexpected response."),
            http_status_code=502,
        )


def _parse_response(data: dict) -> dict:
    """
    Parse the NMI REST v5 sale response.

    NMI uses a classic-style response indicator inside the v5 JSON envelope:
        response      — "1" = approved, "2" = declined, "3" = error
        response_text — human-readable result (also: responsetext)
        id            — NMI transaction ID (also: transactionid)
        response_code — numeric code ("100" = approved)

    Success example:
        {
            "response": "1",
            "id": "9876543210",
            "response_text": "SUCCESS",
            "response_code": "100",
            ...
        }

    Decline example:
        {
            "response": "2",
            "id": "9876543211",
            "response_text": "DECLINE",
            "response_code": "200",
            ...
        }
    """
    response_flag  = str(data.get("response", "3"))
    response_text  = (
        data.get("response_text")
        or data.get("responsetext")
        or data.get("message")
        or ""
    )
    response_code  = str(data.get("response_code", ""))
    # NMI v5 uses "id" for the transaction identifier
    transaction_id = (
        data.get("id")
        or data.get("transaction_id")
        or data.get("transactionid")
        or ""
    )

    _status_map = {"1": "approved", "2": "declined", "3": "error"}
    status = _status_map.get(response_flag, "error")

    is_approved = response_flag == "1"

    if not is_approved:
        frappe.log_error(
            title="NMI Transaction Not Approved",
            message=(
                f"response={response_flag} | status={status} | "
                f"response_code={response_code} | response_text={response_text} | "
                f"transaction_id={transaction_id} | full_response={data}"
            ),
        )

        if status == "declined":
            throw_payment_error(
                ErrorCode.PAYMENT_DECLINED,
                message=_("Your payment was declined. Please check your card details and try again."),
                http_status_code=402,
                nmi_response_text=response_text,
                nmi_response_code=response_code,
            )

        throw_payment_error(
            ErrorCode.PAYMENT_ERROR,
            message=_("Payment could not be processed. Please try again or contact support."),
            http_status_code=402,
            nmi_response_text=response_text,
            nmi_response_code=response_code,
        )

    return {
        "transaction_id": transaction_id,
        "status": status,
        "response_code": response_code,
        "response_text": response_text,
        "amount": float(data.get("amount", 0) or 0),
    }
