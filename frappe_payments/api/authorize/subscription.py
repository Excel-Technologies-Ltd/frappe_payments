"""
Authorize.net ARB (Automated Recurring Billing) subscription endpoint.

Creates a recurring subscription via Authorize.net ARB, then creates a
Sales Invoice and Payment Entry in ERPNext to record the subscription
commitment.

The subscription_id returned by Authorize.net is stored as the Payment
Entry reference_no so the accounting team can reconcile future charges.
Subsequent recurring charges by Authorize.net will appear on the merchant
dashboard and can be reconciled against future invoices manually or via
webhooks.

Workflow
--------
1. Parse & validate inputs.
2. Elevate to Administrator for ERPNext setup:
   a. Resolve or create Customer.
   b. Upsert Billing and Shipping Address records.
   c. Build Sales Invoice in memory → calculate grand_total.
3. Create ARB subscription via Authorize.net (card charged on start_date).
4. On success, back under Administrator:
   a. Insert + Submit the Sales Invoice.
   b. Create + Submit a Payment Entry (mode: Authorize.net,
      ref: SUB-<subscription_id>).
5. Return structured success response.

Endpoint
--------
POST /api/method/frappe_payments.api.authorize.subscription.create_subscription
"""

import json

import frappe
from frappe import _
from frappe.utils import flt, nowdate

from frappe_payments.utils.error_handler import ErrorCode, throw_payment_error, success_response
from frappe_payments.utils import authorize_client
from frappe_payments.api.authorize.charge import (
    _parse_json,
    _validate_items,
    _get_or_create_customer,
    _get_default_company,
    _upsert_address,
    _get_tax_template_for_state,
    _build_invoice,
    _ensure_authorize_mode_of_payment,
    _map_billing,
    _serialize_invoice,
)


@frappe.whitelist(allow_guest=True)
def create_subscription(
    customer_name: str,
    email: str,
    items: "list | str",
    opaque_data_descriptor: str,
    opaque_data_value: str,
    interval_length: int = 1,
    interval_unit: str = "months",
    start_date: str = None,
    total_occurrences: int = 9999,
    trial_amount: float = 0.00,
    trial_occurrences: int = 0,
    subscription_name: str = None,
    phone: str = None,
    billing_address: "dict | str" = None,
    shipping_address: "dict | str" = None,
    notes: str = None,
) -> dict:
    """
    Create a recurring billing subscription via Authorize.net ARB.

    Request body (JSON or form-data):

        customer_name           str   required   Full name of the buyer
        email                   str   required   Email — used to identify/create Customer
        items                   list  required   [{"item_code": "X", "qty": 2}, ...]
        opaque_data_descriptor  str   required   Accept.js dataDescriptor
        opaque_data_value       str   required   Accept.js dataValue (base64 token)
        interval_length         int   optional   Billing interval length (default: 1)
        interval_unit           str   optional   "days" or "months" (default: "months")
        start_date              str   optional   First billing date YYYY-MM-DD (default: today)
        total_occurrences       int   optional   Total billing cycles, 9999 = unlimited (default: 9999)
        trial_amount            float optional   Trial period charge amount (default: 0.00)
        trial_occurrences       int   optional   Number of trial billing cycles (default: 0)
        subscription_name       str   optional   Label shown in Authorize.net dashboard (max 50 chars)
        phone                   str   optional   Contact phone number
        billing_address         dict  optional   {address_line1, city, state, pincode, country}
        shipping_address        dict  optional   Same shape; defaults to billing_address.
        notes                   str   optional   Order notes.

    Response:
        {
          "success": true,
          "message": "Subscription created successfully",
          "data": {
            "subscription_id": "6479745",
            "invoice": {
              "name": "ACC-SINV-2026-00001",
              "customer": "John Doe",
              "status": "Paid",
              "grand_total": 29.99,
              ...
            },
            "payment_entry": "ACC-PAY-2026-00001",
            "interval_length": 1,
            "interval_unit": "months",
            "start_date": "2026-06-14",
            "total_occurrences": 9999
          }
        }
    """
    # --- Deserialise JSON strings sent over HTTP form-data ------------------
    items            = _parse_json(items, "items")
    billing_address  = _parse_json(billing_address,  "billing_address")  if billing_address  else None
    shipping_address = _parse_json(shipping_address, "shipping_address") if shipping_address else None

    # Coerce numeric params that may arrive as strings over form-data
    interval_length   = int(interval_length)   if interval_length   else 1
    total_occurrences = int(total_occurrences) if total_occurrences else 9999
    trial_occurrences = int(trial_occurrences) if trial_occurrences else 0
    trial_amount      = flt(trial_amount)      if trial_amount      else 0.00

    # --- Basic validation ---------------------------------------------------
    _validate_subscription_inputs(
        customer_name, email, opaque_data_descriptor, opaque_data_value,
        items, interval_length, interval_unit, total_occurrences, trial_occurrences,
    )

    # --- ERPNext setup under Administrator ----------------------------------
    _original_user = frappe.session.user
    try:
        frappe.set_user("Administrator")

        _validate_items(items)

        customer = _get_or_create_customer(customer_name, email, phone)
        company  = _get_default_company()

        billing_addr_name  = None
        shipping_addr_name = None
        if billing_address:
            billing_addr_name  = _upsert_address(customer, billing_address, "Billing", email, phone)
            shipping_addr_name = billing_addr_name

        if shipping_address:
            shipping_addr_name = _upsert_address(customer, shipping_address, "Shipping", email, phone)

        addr_for_state = billing_address or shipping_address
        state = addr_for_state.get("state") if addr_for_state else None
        tax_template = _get_tax_template_for_state(state) if state else None

        invoice     = _build_invoice(
            customer=customer,
            company=company,
            items=items,
            billing_address=billing_addr_name,
            shipping_addr_name=shipping_addr_name,
            notes=notes,
            tax_template=tax_template,
        )
        grand_total = flt(invoice.grand_total)

    finally:
        frappe.set_user(_original_user)

    # --- Create ARB subscription via Authorize.net --------------------------
    effective_start = start_date or nowdate()

    subscription = authorize_client.create_subscription(
        amount=grand_total,
        opaque_data_descriptor=opaque_data_descriptor,
        opaque_data_value=opaque_data_value,
        interval_length=interval_length,
        interval_unit=interval_unit,
        start_date=effective_start,
        total_occurrences=total_occurrences,
        trial_amount=trial_amount,
        trial_occurrences=trial_occurrences,
        subscription_name=subscription_name or f"Subscription for {customer_name}",
        billing=_map_billing(billing_address, customer_name, email, phone),
        customer_email=email,
    )

    # --- Persist ERPNext documents ------------------------------------------
    _original_user = frappe.session.user
    try:
        frappe.set_user("Administrator")

        subscription_id = subscription["subscription_id"]

        if notes:
            existing_notes = invoice.terms or ""
            invoice.terms  = f"{existing_notes}\nSubscription ID: {subscription_id}".strip()

        invoice.insert(ignore_permissions=True)
        invoice.submit()

        payment_entry_name = _create_subscription_payment_entry(
            invoice=invoice,
            subscription_id=subscription_id,
            amount=grand_total,
            start_date=effective_start,
            interval_length=interval_length,
            interval_unit=interval_unit,
        )

    except Exception as exc:
        frappe.log_error(
            title="Authorize.net Post-Subscription Document Error",
            message=(
                f"subscription_id={subscription['subscription_id']} | "
                f"amount={grand_total} | customer={customer_name} | "
                f"error={exc}"
            ),
        )
        throw_payment_error(
            ErrorCode.INVOICE_CREATION_FAILED,
            message=_(
                "Your subscription was created (ID: {0}) but we could not "
                "create the order record. Please contact support with this ID."
            ).format(subscription["subscription_id"]),
            http_status_code=500,
            subscription_id=subscription["subscription_id"],
        )
    finally:
        frappe.set_user(_original_user)

    return success_response(
        message=_("Subscription created successfully"),
        data={
            "subscription_id":  subscription["subscription_id"],
            "invoice":          _serialize_invoice(invoice),
            "payment_entry":    payment_entry_name,
            "interval_length":  interval_length,
            "interval_unit":    interval_unit,
            "start_date":       effective_start,
            "total_occurrences": total_occurrences,
        },
    )


@frappe.whitelist(allow_guest=True)
def create_subscription_sandbox(
    customer_name: str,
    email: str,
    items: "list | str",
    card_number: str,
    expiration_month: str,
    expiration_year: str,
    card_code: str = None,
    interval_length: int = 1,
    interval_unit: str = "months",
    start_date: str = None,
    total_occurrences: int = 9999,
    trial_amount: float = 0.00,
    trial_occurrences: int = 0,
    subscription_name: str = None,
    phone: str = None,
    billing_address: "dict | str" = None,
    shipping_address: "dict | str" = None,
    notes: str = None,
) -> dict:
    """
    SANDBOX ONLY — create ARB subscription with raw card data, bypassing Accept.js.

    Identical to create_subscription() but accepts card_number/expiration_month/
    expiration_year/card_code instead of opaque_data_descriptor/opaque_data_value.
    Hard-blocked in Production by authorize_client.create_subscription_with_card().
    """
    from frappe_payments.utils import authorize_client

    items            = _parse_json(items, "items")
    billing_address  = _parse_json(billing_address,  "billing_address")  if billing_address  else None
    shipping_address = _parse_json(shipping_address, "shipping_address") if shipping_address else None

    interval_length   = int(interval_length)   if interval_length   else 1
    total_occurrences = int(total_occurrences) if total_occurrences else 9999
    trial_occurrences = int(trial_occurrences) if trial_occurrences else 0
    trial_amount      = flt(trial_amount)      if trial_amount      else 0.00

    if not customer_name:
        frappe.throw(_("customer_name is required"), frappe.MandatoryError)
    if not email or "@" not in email:
        frappe.throw(_("A valid email address is required"), frappe.ValidationError)
    if not card_number or not expiration_month or not expiration_year:
        throw_payment_error(ErrorCode.INVALID_PAYMENT_TOKEN, http_status_code=400)
    if not items or not isinstance(items, list):
        frappe.throw(_("items must be a non-empty list"), frappe.MandatoryError)
    for idx, row in enumerate(items, start=1):
        if not row.get("item_code"):
            frappe.throw(_("Row {0}: item_code is required").format(idx), frappe.MandatoryError)
        if flt(row.get("qty", 0)) <= 0:
            frappe.throw(_("Row {0}: qty must be greater than 0").format(idx), frappe.ValidationError)
    if interval_length < 1 or interval_length > 999:
        frappe.throw(_("interval_length must be between 1 and 999"), frappe.ValidationError)
    if interval_unit not in ("days", "months"):
        frappe.throw(_("interval_unit must be 'days' or 'months'"), frappe.ValidationError)

    expiration_date   = f"{str(expiration_year)}-{str(expiration_month).zfill(2)}"
    effective_start   = start_date or nowdate()

    from frappe_payments.api.authorize.charge import (
        _get_or_create_customer, _get_default_company, _upsert_address,
        _get_tax_template_for_state, _build_invoice, _map_billing, _serialize_invoice,
    )

    _original_user = frappe.session.user
    try:
        frappe.set_user("Administrator")
        from frappe_payments.api.authorize.charge import _validate_items as _vi
        _vi(items)
        customer = _get_or_create_customer(customer_name, email, phone)
        company  = _get_default_company()

        billing_addr_name  = None
        shipping_addr_name = None
        if billing_address:
            billing_addr_name  = _upsert_address(customer, billing_address, "Billing", email, phone)
            shipping_addr_name = billing_addr_name
        if shipping_address:
            shipping_addr_name = _upsert_address(customer, shipping_address, "Shipping", email, phone)

        addr_for_state = billing_address or shipping_address
        state = addr_for_state.get("state") if addr_for_state else None
        tax_template = _get_tax_template_for_state(state) if state else None

        invoice     = _build_invoice(
            customer=customer,
            company=company,
            items=items,
            billing_address=billing_addr_name,
            shipping_addr_name=shipping_addr_name,
            notes=notes,
            tax_template=tax_template,
        )
        grand_total = flt(invoice.grand_total)
    finally:
        frappe.set_user(_original_user)

    subscription = authorize_client.create_subscription_with_card(
        amount=grand_total,
        card_number=card_number.replace(" ", ""),
        expiration_date=expiration_date,
        card_code=card_code or None,
        interval_length=interval_length,
        interval_unit=interval_unit,
        start_date=effective_start,
        total_occurrences=total_occurrences,
        trial_amount=trial_amount,
        trial_occurrences=trial_occurrences,
        subscription_name=subscription_name or f"Subscription for {customer_name}",
        billing=_map_billing(billing_address, customer_name, email, phone),
        customer_email=email,
    )

    _original_user = frappe.session.user
    try:
        frappe.set_user("Administrator")
        subscription_id = subscription["subscription_id"]
        if notes:
            invoice.terms = f"{invoice.terms or ''}\nSubscription ID: {subscription_id}".strip()
        invoice.insert(ignore_permissions=True)
        invoice.submit()
        payment_entry_name = _create_subscription_payment_entry(
            invoice=invoice,
            subscription_id=subscription_id,
            amount=grand_total,
            start_date=effective_start,
            interval_length=interval_length,
            interval_unit=interval_unit,
        )
    except Exception as exc:
        frappe.log_error(
            title="Authorize.net Post-Subscription Document Error (Sandbox)",
            message=(
                f"subscription_id={subscription['subscription_id']} | "
                f"amount={grand_total} | customer={customer_name} | error={exc}"
            ),
        )
        throw_payment_error(
            ErrorCode.INVOICE_CREATION_FAILED,
            message=_(
                "Your subscription was created (ID: {0}) but we could not "
                "create the order record. Please contact support with this ID."
            ).format(subscription["subscription_id"]),
            http_status_code=500,
            subscription_id=subscription["subscription_id"],
        )
    finally:
        frappe.set_user(_original_user)

    return success_response(
        message=_("Subscription created successfully"),
        data={
            "subscription_id":  subscription["subscription_id"],
            "invoice":          _serialize_invoice(invoice),
            "payment_entry":    payment_entry_name,
            "interval_length":  interval_length,
            "interval_unit":    interval_unit,
            "start_date":       effective_start,
            "total_occurrences": total_occurrences,
        },
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_subscription_inputs(
    customer_name, email, opaque_data_descriptor, opaque_data_value,
    items, interval_length, interval_unit, total_occurrences, trial_occurrences,
):
    if not customer_name:
        frappe.throw(_("customer_name is required"), frappe.MandatoryError)

    if not email or "@" not in email:
        frappe.throw(_("A valid email address is required"), frappe.ValidationError)

    if not opaque_data_descriptor or not opaque_data_value:
        throw_payment_error(ErrorCode.INVALID_PAYMENT_TOKEN, http_status_code=400)

    if not items or not isinstance(items, list):
        frappe.throw(_("items must be a non-empty list"), frappe.MandatoryError)

    for idx, row in enumerate(items, start=1):
        if not row.get("item_code"):
            frappe.throw(_("Row {0}: item_code is required").format(idx), frappe.MandatoryError)
        if flt(row.get("qty", 0)) <= 0:
            frappe.throw(
                _("Row {0}: qty must be greater than 0").format(idx), frappe.ValidationError
            )

    if interval_length < 1 or interval_length > 999:
        frappe.throw(_("interval_length must be between 1 and 999"), frappe.ValidationError)

    if interval_unit not in ("days", "months"):
        frappe.throw(_("interval_unit must be 'days' or 'months'"), frappe.ValidationError)

    if interval_unit == "days" and interval_length > 365:
        frappe.throw(
            _("interval_length cannot exceed 365 days for a daily subscription"),
            frappe.ValidationError,
        )

    if total_occurrences < 1:
        frappe.throw(_("total_occurrences must be at least 1"), frappe.ValidationError)

    if trial_occurrences < 0:
        frappe.throw(_("trial_occurrences cannot be negative"), frappe.ValidationError)


# ---------------------------------------------------------------------------
# Payment Entry
# ---------------------------------------------------------------------------

def _create_subscription_payment_entry(
    invoice: "frappe.model.document.Document",
    subscription_id: str,
    amount: float,
    start_date: str,
    interval_length: int,
    interval_unit: str,
) -> str:
    """
    Create and submit a Payment Entry for the first subscription period.

    Uses the Authorize.net subscription_id as reference_no so the accounting
    team can correlate ERPNext entries with the Authorize.net dashboard.
    """
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    _ensure_authorize_mode_of_payment()

    pe = get_payment_entry("Sales Invoice", invoice.name, party_amount=amount)
    pe.mode_of_payment = "Authorize.net"
    pe.reference_no    = f"SUB-{subscription_id}"
    pe.reference_date  = nowdate()
    pe.remarks         = (
        f"Authorize.net subscription {subscription_id} for {invoice.name} | "
        f"Recurring every {interval_length} {interval_unit} starting {start_date}"
    )
    pe.insert(ignore_permissions=True)
    pe.submit()

    return pe.name
