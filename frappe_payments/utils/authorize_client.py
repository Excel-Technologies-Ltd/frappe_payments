"""
HTTP client for the Authorize.net JSON API.

Docs:    https://developer.authorize.net/api/reference/index.html
Accept.js: https://developer.authorize.net/api/reference/features/acceptjs.html
ARB:     https://developer.authorize.net/api/reference/index.html#recurring-billing

Endpoints (same URL for all operations):
  Sandbox:    https://apitest.authorize.net/xml/v1/request.api
  Production: https://api.authorize.net/xml/v1/request.api

Auth: merchantAuthentication { name: api_login_id, transactionKey: transaction_key }
Format: JSON request / JSON response

One-time charge:
  Request type: createTransactionRequest (authCaptureTransaction)
  Approval indicator: transactionResponse.responseCode == "1"
  Transaction ID: transactionResponse.transId

Recurring billing (ARB):
  Request type: ARBCreateSubscriptionRequest
  Approval indicator: messages.resultCode == "Ok"
  Subscription ID: subscriptionId

Note: Authorize.net occasionally returns JSON with a UTF-8 BOM prefix (﻿).
The _post() function strips it before parsing.
"""

import json

import frappe
import requests
from frappe import _

from frappe_payments.utils.error_handler import ErrorCode, throw_payment_error
from frappe_payments.utils.payment_settings import get_authorize_settings

_REQUEST_TIMEOUT = 30  # seconds


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def charge(
    *,
    amount: float,
    opaque_data_descriptor: str,
    opaque_data_value: str,
    billing: dict = None,
    order_id: str = None,
    description: str = None,
    customer_email: str = None,
) -> dict:
    """
    Create an authCaptureTransaction (authorize + capture) via Authorize.net.

    Args:
        amount:                  Charge amount (e.g. 99.99).
        opaque_data_descriptor:  Token descriptor from Accept.js
                                   (e.g. "COMMON.ACCEPT.INAPP.PAYMENT").
        opaque_data_value:       Token value from Accept.js (base64 blob).
        billing:                 Optional billing info:
                                   {first_name, last_name, address, city,
                                    state, zip, country}
        order_id:                Reference ID (max 20 chars) shown in dashboard.
        description:             Order description (max 255 chars).
        customer_email:          Customer email for receipt.

    Returns:
        {
            "transaction_id": str,
            "status": str,          # "approved" | "declined" | "error" | "held"
            "response_code": str,
            "response_text": str,
            "amount": float,
        }

    Raises frappe.ValidationError on any gateway or transport error.
    """
    settings = _get_active_settings()
    payload = _build_charge_payload(
        settings=settings,
        amount=amount,
        opaque_data_descriptor=opaque_data_descriptor,
        opaque_data_value=opaque_data_value,
        billing=billing,
        order_id=order_id,
        description=description,
        customer_email=customer_email,
    )
    data = _post(settings["base_url"], payload)
    return _parse_charge_response(data)


def charge_with_card(
    *,
    amount: float,
    card_number: str,
    expiration_date: str,
    card_code: str = None,
    billing: dict = None,
    order_id: str = None,
    description: str = None,
    customer_email: str = None,
) -> dict:
    """
    SANDBOX ONLY — charge with raw card data, bypassing Accept.js.

    Accept.js enforces HTTPS and cannot be used on HTTP development servers.
    This function submits card data directly to Authorize.net, which is only
    safe in Sandbox. The gateway client hard-blocks this in Production.

    Args:
        amount:           Charge amount.
        card_number:      Raw card number (digits only, no spaces).
        expiration_date:  "YYYY-MM" format (e.g. "2029-12").
        card_code:        CVV/CVC (optional but recommended).
        billing:          Optional billing info dict.
        order_id:         Reference ID (max 20 chars).
        description:      Order description.
        customer_email:   Customer email for receipt.

    Returns same dict as charge().
    """
    settings = _get_active_settings()

    if settings.get("environment") != "Sandbox":
        frappe.throw(
            _("Direct card charge is only allowed in Sandbox environment."),
            frappe.PermissionError,
        )

    payload = _build_card_charge_payload(
        settings=settings,
        amount=amount,
        card_number=card_number,
        expiration_date=expiration_date,
        card_code=card_code,
        billing=billing,
        order_id=order_id,
        description=description,
        customer_email=customer_email,
    )
    data = _post(settings["base_url"], payload)
    return _parse_charge_response(data)


def create_subscription_with_card(
    *,
    amount: float,
    card_number: str,
    expiration_date: str,
    card_code: str = None,
    interval_length: int = 1,
    interval_unit: str = "months",
    start_date: str = None,
    total_occurrences: int = 9999,
    trial_amount: float = 0.00,
    trial_occurrences: int = 0,
    subscription_name: str = None,
    billing: dict = None,
    customer_email: str = None,
) -> dict:
    """
    SANDBOX ONLY — create ARB subscription with raw card data, bypassing Accept.js.
    """
    from frappe.utils import nowdate

    settings = _get_active_settings()

    if settings.get("environment") != "Sandbox":
        frappe.throw(
            _("Direct card subscription is only allowed in Sandbox environment."),
            frappe.PermissionError,
        )

    credit_card_block = {"cardNumber": card_number, "expirationDate": expiration_date}
    if card_code:
        credit_card_block["cardCode"] = card_code

    payment_schedule = {
        "interval": {"length": str(interval_length), "unit": interval_unit},
        "startDate": start_date or nowdate(),
        "totalOccurrences": str(total_occurrences),
    }
    if trial_occurrences > 0:
        payment_schedule["trialOccurrences"] = str(trial_occurrences)

    subscription = {
        "paymentSchedule": payment_schedule,
        "amount": f"{float(amount):.2f}",
        "payment": {"creditCard": credit_card_block},
    }

    if subscription_name:
        subscription["name"] = str(subscription_name)[:50]

    if trial_occurrences > 0:
        subscription["trialAmount"] = f"{float(trial_amount):.2f}"

    # "customer" must appear before "billTo" in Authorize.net's schema
    if customer_email:
        subscription["customer"] = {"email": customer_email}

    if billing:
        bill_to = {k: v for k, v in {
            "firstName": billing.get("first_name", ""),
            "lastName":  billing.get("last_name", ""),
            "address":   billing.get("address", ""),
            "city":      billing.get("city", ""),
            "state":     billing.get("state", ""),
            "zip":       billing.get("zip", ""),
            "country":   billing.get("country", "US"),
        }.items() if v}
        if bill_to:
            subscription["billTo"] = bill_to

    payload = {
        "ARBCreateSubscriptionRequest": {
            "merchantAuthentication": _merchant_auth(settings),
            "subscription": subscription,
        }
    }
    data = _post(settings["base_url"], payload)
    return _parse_subscription_response(data)


def create_subscription(
    *,
    amount: float,
    opaque_data_descriptor: str,
    opaque_data_value: str,
    interval_length: int = 1,
    interval_unit: str = "months",
    start_date: str = None,
    total_occurrences: int = 9999,
    trial_amount: float = 0.00,
    trial_occurrences: int = 0,
    subscription_name: str = None,
    billing: dict = None,
    customer_email: str = None,
) -> dict:
    """
    Create an ARB (Automated Recurring Billing) subscription via Authorize.net.

    Args:
        amount:                  Recurring charge amount per billing cycle.
        opaque_data_descriptor:  Token descriptor from Accept.js.
        opaque_data_value:       Token value from Accept.js.
        interval_length:         Billing interval length (1-999). Default: 1.
        interval_unit:           "days" or "months". Default: "months".
        start_date:              First billing date (YYYY-MM-DD). Default: today.
        total_occurrences:       Total billing cycles. Use 9999 for unlimited.
        trial_amount:            Charge amount during trial period. Default: 0.00.
        trial_occurrences:       Number of trial billing cycles. Default: 0.
        subscription_name:       Descriptive label (max 50 chars).
        billing:                 Billing info dict (first_name, last_name, address, …).
        customer_email:          Customer email.

    Returns:
        {
            "subscription_id": str,
            "status": str,       # "active"
            "message": str,
        }

    Raises frappe.ValidationError on failure.
    """
    from frappe.utils import nowdate

    settings = _get_active_settings()
    payload = _build_subscription_payload(
        settings=settings,
        amount=amount,
        opaque_data_descriptor=opaque_data_descriptor,
        opaque_data_value=opaque_data_value,
        interval_length=interval_length,
        interval_unit=interval_unit,
        start_date=start_date or nowdate(),
        total_occurrences=total_occurrences,
        trial_amount=trial_amount,
        trial_occurrences=trial_occurrences,
        subscription_name=subscription_name,
        billing=billing,
        customer_email=customer_email,
    )
    data = _post(settings["base_url"], payload)
    return _parse_subscription_response(data)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_active_settings() -> dict:
    settings = get_authorize_settings()

    if not settings.get("enabled"):
        throw_payment_error(ErrorCode.GATEWAY_DISABLED, http_status_code=503)

    if not settings.get("api_login_id"):
        frappe.log_error(
            title="Authorize Settings - API Login ID Missing",
            message="API Login ID is empty. Open Authorize Settings and re-save.",
        )
        throw_payment_error(ErrorCode.GATEWAY_NOT_CONFIGURED, http_status_code=503)

    if not settings.get("transaction_key"):
        frappe.log_error(
            title="Authorize Settings - Transaction Key Missing",
            message="Transaction Key is empty. Open Authorize Settings and re-save.",
        )
        throw_payment_error(ErrorCode.GATEWAY_NOT_CONFIGURED, http_status_code=503)

    return settings


def _merchant_auth(settings: dict) -> dict:
    return {
        "name": settings["api_login_id"],
        "transactionKey": settings["transaction_key"],
    }


def _build_card_charge_payload(
    *,
    settings: dict,
    amount: float,
    card_number: str,
    expiration_date: str,
    card_code: str,
    billing: dict,
    order_id: str,
    description: str,
    customer_email: str,
) -> dict:
    """Build createTransactionRequest payload using raw credit card data (sandbox only)."""
    credit_card = {
        "cardNumber": card_number,
        "expirationDate": expiration_date,
    }
    if card_code:
        credit_card["cardCode"] = card_code

    transaction_request = {
        "transactionType": "authCaptureTransaction",
        "amount": f"{float(amount):.2f}",
        "payment": {"creditCard": credit_card},
    }

    if order_id or description:
        order_block = {}
        if order_id:
            order_block["invoiceNumber"] = str(order_id)[:20]
        if description:
            order_block["description"] = str(description)[:255]
        transaction_request["order"] = order_block

    # "customer" must appear before "billTo" in Authorize.net's schema
    if customer_email:
        transaction_request["customer"] = {"email": customer_email}

    if billing:
        bill_to = {k: v for k, v in {
            "firstName": billing.get("first_name", ""),
            "lastName":  billing.get("last_name", ""),
            "address":   billing.get("address", ""),
            "city":      billing.get("city", ""),
            "state":     billing.get("state", ""),
            "zip":       billing.get("zip", ""),
            "country":   billing.get("country", "US"),
        }.items() if v}
        if bill_to:
            transaction_request["billTo"] = bill_to

    return {
        "createTransactionRequest": {
            "merchantAuthentication": _merchant_auth(settings),
            "transactionRequest": transaction_request,
        }
    }


def _build_charge_payload(
    *,
    settings: dict,
    amount: float,
    opaque_data_descriptor: str,
    opaque_data_value: str,
    billing: dict,
    order_id: str,
    description: str,
    customer_email: str,
) -> dict:
    """
    Build the createTransactionRequest JSON payload.

    Minimal required payload:
        {
            "createTransactionRequest": {
                "merchantAuthentication": {...},
                "transactionRequest": {
                    "transactionType": "authCaptureTransaction",
                    "amount": "99.99",
                    "payment": {
                        "opaqueData": {
                            "dataDescriptor": "COMMON.ACCEPT.INAPP.PAYMENT",
                            "dataValue": "<base64 token>"
                        }
                    }
                }
            }
        }
    """
    transaction_request = {
        "transactionType": "authCaptureTransaction",
        "amount": f"{float(amount):.2f}",
        "payment": {
            "opaqueData": {
                "dataDescriptor": opaque_data_descriptor,
                "dataValue": opaque_data_value,
            }
        },
    }

    if order_id or description:
        order_block = {}
        if order_id:
            order_block["invoiceNumber"] = str(order_id)[:20]
        if description:
            order_block["description"] = str(description)[:255]
        transaction_request["order"] = order_block

    # "customer" must appear before "billTo" in Authorize.net's schema
    if customer_email:
        transaction_request["customer"] = {"email": customer_email}

    if billing:
        bill_to = {k: v for k, v in {
            "firstName": billing.get("first_name", ""),
            "lastName":  billing.get("last_name", ""),
            "address":   billing.get("address", ""),
            "city":      billing.get("city", ""),
            "state":     billing.get("state", ""),
            "zip":       billing.get("zip", ""),
            "country":   billing.get("country", "US"),
        }.items() if v}
        if bill_to:
            transaction_request["billTo"] = bill_to

    return {
        "createTransactionRequest": {
            "merchantAuthentication": _merchant_auth(settings),
            "transactionRequest": transaction_request,
        }
    }


def _build_subscription_payload(
    *,
    settings: dict,
    amount: float,
    opaque_data_descriptor: str,
    opaque_data_value: str,
    interval_length: int,
    interval_unit: str,
    start_date: str,
    total_occurrences: int,
    trial_amount: float,
    trial_occurrences: int,
    subscription_name: str,
    billing: dict,
    customer_email: str,
) -> dict:
    """
    Build the ARBCreateSubscriptionRequest JSON payload.

    The subscription will be charged on start_date and then every
    interval_length interval_units until total_occurrences are complete.
    """
    payment_schedule = {
        "interval": {
            "length": str(interval_length),
            "unit": interval_unit,
        },
        "startDate": start_date,
        "totalOccurrences": str(total_occurrences),
    }
    if trial_occurrences > 0:
        payment_schedule["trialOccurrences"] = str(trial_occurrences)

    subscription = {
        "paymentSchedule": payment_schedule,
        "amount": f"{float(amount):.2f}",
        "payment": {
            "opaqueData": {
                "dataDescriptor": opaque_data_descriptor,
                "dataValue": opaque_data_value,
            }
        },
    }

    if subscription_name:
        subscription["name"] = str(subscription_name)[:50]

    if trial_occurrences > 0:
        subscription["trialAmount"] = f"{float(trial_amount):.2f}"

    # "customer" must appear before "billTo" in Authorize.net's schema
    if customer_email:
        subscription["customer"] = {"email": customer_email}

    if billing:
        bill_to = {k: v for k, v in {
            "firstName": billing.get("first_name", ""),
            "lastName":  billing.get("last_name", ""),
            "address":   billing.get("address", ""),
            "city":      billing.get("city", ""),
            "state":     billing.get("state", ""),
            "zip":       billing.get("zip", ""),
            "country":   billing.get("country", "US"),
        }.items() if v}
        if bill_to:
            subscription["billTo"] = bill_to

    return {
        "ARBCreateSubscriptionRequest": {
            "merchantAuthentication": _merchant_auth(settings),
            "subscription": subscription,
        }
    }


def _post(base_url: str, payload: dict) -> dict:
    """POST JSON to the Authorize.net endpoint and return parsed response."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        resp = requests.post(base_url, json=payload, headers=headers, timeout=_REQUEST_TIMEOUT)
    except requests.Timeout:
        frappe.log_error(
            title="Authorize.net Gateway Timeout",
            message=f"POST {base_url} timed out after {_REQUEST_TIMEOUT}s",
        )
        throw_payment_error(
            ErrorCode.PAYMENT_ERROR,
            message=_("Payment gateway timed out. Please try again."),
            http_status_code=504,
        )
    except requests.RequestException as exc:
        frappe.log_error(title="Authorize.net Gateway Connection Error", message=str(exc))
        throw_payment_error(
            ErrorCode.PAYMENT_ERROR,
            message=_("Could not reach the payment gateway. Please try again."),
            http_status_code=502,
        )

    # Work at the bytes level so BOM stripping is exact and encoding issues
    # cannot produce an empty string from a non-empty response.
    # Authorize.net prepends a UTF-8 BOM (\xef\xbb\xbf) to some responses.
    raw = resp.content
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    text = raw.decode("utf-8", errors="replace").strip()

    # Always log the raw response for auditability
    frappe.log_error(
        title="Authorize.net Raw Response",
        message=(
            f"HTTP {resp.status_code} | "
            f"Content-Type: {resp.headers.get('Content-Type', 'n/a')}\n"
            f"{text[:3000]}"
        ),
    )

    if not text:
        frappe.log_error(
            title="Authorize.net Empty Response Body",
            message=f"HTTP {resp.status_code} — gateway returned no body.",
        )
        throw_payment_error(
            ErrorCode.PAYMENT_ERROR,
            message=_("The payment gateway returned an empty response. Please try again."),
            http_status_code=502,
        )

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        frappe.log_error(
            title="Authorize.net Non-JSON Response",
            message=f"HTTP {resp.status_code}: {text[:1000]}",
        )
        throw_payment_error(
            ErrorCode.PAYMENT_ERROR,
            message=_("The payment gateway returned an unexpected response."),
            http_status_code=502,
        )


def _parse_charge_response(data: dict) -> dict:
    """
    Parse createTransactionRequest response.

    Authorize.net uses two-level approval:
      1. messages.resultCode == "Ok"  — API call itself succeeded
      2. transactionResponse.responseCode == "1"  — transaction approved

    responseCode meanings:
      "1" = approved
      "2" = declined
      "3" = error
      "4" = held for review
    """
    messages = data.get("messages", {})
    result_code = messages.get("resultCode", "Error")

    tx = data.get("transactionResponse", {})
    response_code = str(tx.get("responseCode") or "3")
    transaction_id = str(tx.get("transId") or "")

    # Extract the most useful human-readable message
    tx_messages = tx.get("messages") or []
    tx_errors   = tx.get("errors") or []
    top_messages = messages.get("message") or []

    if tx_messages:
        response_text = tx_messages[0].get("description", "")
    elif tx_errors:
        response_text = tx_errors[0].get("errorText", "")
    elif top_messages:
        response_text = top_messages[0].get("text", "")
    else:
        response_text = ""

    _status_map = {"1": "approved", "2": "declined", "3": "error", "4": "held"}
    status = _status_map.get(response_code, "error")

    is_approved = response_code == "1" and result_code == "Ok"

    if not is_approved:
        frappe.log_error(
            title="Authorize.net Transaction Not Approved",
            message=(
                f"resultCode={result_code} | responseCode={response_code} | "
                f"status={status} | response_text={response_text} | "
                f"transaction_id={transaction_id} | full_response={data}"
            ),
        )

        if status == "declined":
            throw_payment_error(
                ErrorCode.PAYMENT_DECLINED,
                message=_("Your payment was declined. Please check your card details and try again."),
                http_status_code=402,
                authorize_response_text=response_text,
                authorize_response_code=response_code,
            )

        throw_payment_error(
            ErrorCode.PAYMENT_ERROR,
            message=_("Payment could not be processed. Please try again or contact support."),
            http_status_code=402,
            authorize_response_text=response_text,
            authorize_response_code=response_code,
        )

    settled_amount = tx.get("settleAmount") or tx.get("requestedAmount") or 0
    return {
        "transaction_id": transaction_id,
        "status": status,
        "response_code": response_code,
        "response_text": response_text,
        "amount": float(settled_amount),
    }


def _parse_subscription_response(data: dict) -> dict:
    """
    Parse ARBCreateSubscriptionRequest response.

    Success: messages.resultCode == "Ok" and subscriptionId is present.
    """
    messages = data.get("messages", {})
    result_code = messages.get("resultCode", "Error")
    subscription_id = str(data.get("subscriptionId") or "")

    top_messages = messages.get("message") or []
    message_text = top_messages[0].get("text", "") if top_messages else ""

    if result_code != "Ok" or not subscription_id:
        frappe.log_error(
            title="Authorize.net Subscription Creation Failed",
            message=(
                f"resultCode={result_code} | subscriptionId={subscription_id} | "
                f"message={message_text} | full_response={data}"
            ),
        )
        throw_payment_error(
            ErrorCode.PAYMENT_ERROR,
            message=_("Failed to create subscription. Please try again or contact support."),
            http_status_code=402,
            authorize_response_text=message_text,
        )

    return {
        "subscription_id": subscription_id,
        "status": "active",
        "message": message_text,
    }
