"""
Authorize.net one-time charge endpoint — full eCommerce payment flow.

Mirrors frappe_payments.api.nmi.charge exactly, replacing NMI-specific calls
with Authorize.net equivalents.

Workflow
--------
1. Parse & validate inputs (items, email, opaque_data_descriptor/value).
2. Elevate to Administrator to run ERPNext operations:
   a. Resolve or create Customer (by email).
   b. Upsert Billing and Shipping Address records.
   c. Build Sales Invoice in memory → calculate grand_total (no DB write yet).
3. Charge grand_total via Authorize.net using the Accept.js opaque data token.
4. On approval, back under Administrator:
   a. Insert + Submit the Sales Invoice.
   b. Create + Submit a Payment Entry (mode: Authorize.net, ref: transaction_id).
5. Return structured success response.

Endpoint
--------
POST /api/method/frappe_payments.api.authorize.charge.charge
"""

import json

import frappe
from frappe import _
from frappe.utils import flt, nowdate

from frappe_payments.utils.error_handler import ErrorCode, throw_payment_error, success_response
from frappe_payments.utils import authorize_client
from frappe_payments.utils.coupon import apply_coupon_to_invoice


@frappe.whitelist(allow_guest=True)
def charge(
    customer_name: str,
    email: str,
    items: "list | str",
    opaque_data_descriptor: str,
    opaque_data_value: str,
    phone: str = None,
    billing_address: "dict | str" = None,
    shipping_address: "dict | str" = None,
    notes: str = None,
    coupon_code: str = None,
) -> dict:
    """
    Charge a card via Authorize.net and create all ERPNext documents in one call.

    Works for both guest and authenticated users.

    Request body (JSON or form-data):

        customer_name           str   required   Full name of the buyer
        email                   str   required   Email — used to identify/create Customer
        items                   list  required   [{"item_code": "X", "qty": 2}, ...]
                                                  Optional "rate" key overrides price list.
        opaque_data_descriptor  str   required   Accept.js dataDescriptor
                                                  (e.g. "COMMON.ACCEPT.INAPP.PAYMENT")
        opaque_data_value       str   required   Accept.js dataValue (base64 token)
        phone                   str   optional   Contact phone number
        billing_address         dict  optional   {
                                                   "address_line1": "123 Main St",
                                                   "address_line2": "",
                                                   "city": "New York",
                                                   "state": "NY",
                                                   "pincode": "10001",
                                                   "country": "United States"
                                                 }
        shipping_address        dict  optional   Same shape; defaults to billing_address.
        notes                   str   optional   Order notes / delivery instructions.

    Response:
        {
          "success": true,
          "message": "Payment successful",
          "data": {
            "transaction_id": "60015797839",
            "invoice": {
              "name": "ACC-SINV-2026-00001",
              "customer": "John Doe",
              "status": "Paid",
              "grand_total": 199.98,
              ...
            },
            "payment_entry": "ACC-PAY-2026-00001"
          }
        }
    """
    # --- Deserialise JSON strings sent over HTTP form-data ------------------
    items            = _parse_json(items, "items")
    billing_address  = _parse_json(billing_address,  "billing_address")  if billing_address  else None
    shipping_address = _parse_json(shipping_address, "shipping_address") if shipping_address else None

    # --- Basic validation ---------------------------------------------------
    _validate_basic(customer_name, email, opaque_data_descriptor, opaque_data_value, items)

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
        discount_info = apply_coupon_to_invoice(invoice, coupon_code, customer) if coupon_code else None
        grand_total = flt(invoice.grand_total)

    finally:
        frappe.set_user(_original_user)

    if not grand_total:
        frappe.throw(
            "Order total is zero. Ensure items have a price list rate or pass a rate explicitly.",
            frappe.ValidationError,
        )

    # --- Charge the card via Authorize.net ----------------------------------
    transaction = authorize_client.charge(
        amount=grand_total,
        opaque_data_descriptor=opaque_data_descriptor,
        opaque_data_value=opaque_data_value,
        billing=_map_billing(billing_address, customer_name, email, phone),
        order_id=None,
        description=f"Order for {customer_name}",
        customer_email=email,
    )

    # --- Persist ERPNext documents after confirmed payment ------------------
    _original_user = frappe.session.user
    try:
        frappe.set_user("Administrator")

        invoice.insert(ignore_permissions=True)
        invoice.submit()

        payment_entry_name = _create_payment_entry(
            invoice=invoice,
            transaction_id=transaction["transaction_id"],
        )

    except Exception as exc:
        frappe.log_error(
            title="Authorize.net Post-Charge Document Error",
            message=(
                f"transaction_id={transaction['transaction_id']} | "
                f"amount={grand_total} | customer={customer_name} | "
                f"error={exc}"
            ),
        )
        throw_payment_error(
            ErrorCode.INVOICE_CREATION_FAILED,
            message=_(
                "Your payment was received (transaction ID: {0}) but we could not "
                "create the order record. Please contact support with this ID."
            ).format(transaction["transaction_id"]),
            http_status_code=500,
            transaction_id=transaction["transaction_id"],
        )
    finally:
        frappe.set_user(_original_user)

    resp_data = {
        "transaction_id": transaction["transaction_id"],
        "invoice":        _serialize_invoice(invoice),
        "payment_entry":  payment_entry_name,
    }
    if discount_info:
        resp_data["coupon"] = discount_info
    return success_response(message=_("Payment successful"), data=resp_data)


@frappe.whitelist(allow_guest=True)
def charge_sandbox(
    customer_name: str,
    email: str,
    items: "list | str",
    card_number: str,
    expiration_month: str,
    expiration_year: str,
    card_code: str = None,
    phone: str = None,
    billing_address: "dict | str" = None,
    shipping_address: "dict | str" = None,
    notes: str = None,
    coupon_code: str = None,
) -> dict:
    """
    SANDBOX ONLY — one-time charge with raw card data, bypassing Accept.js.

    Accept.js requires HTTPS and cannot run on HTTP development servers.
    This endpoint accepts card details directly and is hard-blocked in
    Production by authorize_client.charge_with_card().

    Request body fields are identical to charge() except:
      card_number       str  required  Raw card number (digits)
      expiration_month  str  required  "MM"
      expiration_year   str  required  "YYYY"
      card_code         str  optional  CVV
    """
    items            = _parse_json(items, "items")
    billing_address  = _parse_json(billing_address,  "billing_address")  if billing_address  else None
    shipping_address = _parse_json(shipping_address, "shipping_address") if shipping_address else None

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

    expiration_date = f"{str(expiration_year)}-{str(expiration_month).zfill(2)}"

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
        discount_info = apply_coupon_to_invoice(invoice, coupon_code, customer) if coupon_code else None
        grand_total = flt(invoice.grand_total)
    finally:
        frappe.set_user(_original_user)

    if not grand_total:
        frappe.throw(
            "Order total is zero. Ensure items have a price list rate or pass a rate explicitly.",
            frappe.ValidationError,
        )

    transaction = authorize_client.charge_with_card(
        amount=grand_total,
        card_number=card_number.replace(" ", ""),
        expiration_date=expiration_date,
        card_code=card_code or None,
        billing=_map_billing(billing_address, customer_name, email, phone),
        description=f"Order for {customer_name}",
        customer_email=email,
    )

    _original_user = frappe.session.user
    try:
        frappe.set_user("Administrator")
        invoice.insert(ignore_permissions=True)
        invoice.submit()
        payment_entry_name = _create_payment_entry(
            invoice=invoice,
            transaction_id=transaction["transaction_id"],
        )
    except Exception as exc:
        frappe.log_error(
            title="Authorize.net Post-Charge Document Error (Sandbox)",
            message=(
                f"transaction_id={transaction['transaction_id']} | "
                f"amount={grand_total} | customer={customer_name} | error={exc}"
            ),
        )
        throw_payment_error(
            ErrorCode.INVOICE_CREATION_FAILED,
            message=_(
                "Your payment was received (transaction ID: {0}) but we could not "
                "create the order record. Please contact support with this ID."
            ).format(transaction["transaction_id"]),
            http_status_code=500,
            transaction_id=transaction["transaction_id"],
        )
    finally:
        frappe.set_user(_original_user)

    resp_data = {
        "transaction_id": transaction["transaction_id"],
        "invoice":        _serialize_invoice(invoice),
        "payment_entry":  payment_entry_name,
    }
    if discount_info:
        resp_data["coupon"] = discount_info
    return success_response(message=_("Payment successful"), data=resp_data)


@frappe.whitelist(allow_guest=True)
def get_public_config() -> dict:
    """
    Return the public-facing Authorize.net configuration needed by Accept.js.

    Exposes only non-sensitive values (client_key, api_login_id, environment).
    Neither the Transaction Key nor the API Login ID (server secret) is included —
    only the Client Key and API Login ID which are required by the browser SDK.
    """
    from frappe_payments.utils.payment_settings import get_authorize_settings
    settings = get_authorize_settings()
    return {
        "api_login_id": settings.get("api_login_id", ""),
        "client_key":   settings.get("client_key", ""),
        "environment":  settings.get("environment", "Sandbox"),
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_basic(
    customer_name: str,
    email: str,
    opaque_data_descriptor: str,
    opaque_data_value: str,
    items: list,
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


def _validate_items(items: list):
    for idx, row in enumerate(items, start=1):
        if not frappe.db.exists("Item", {"name": row["item_code"], "disabled": 0}):
            frappe.throw(
                _("Row {0}: Item '{1}' does not exist or is disabled").format(
                    idx, row["item_code"]
                ),
                frappe.ValidationError,
            )


# ---------------------------------------------------------------------------
# Customer resolution
# ---------------------------------------------------------------------------

def _get_or_create_customer(customer_name: str, email: str, phone: str = None) -> str:
    if frappe.session.user not in ("Guest", "Administrator"):
        linked = frappe.db.get_value("Customer", {"email_id": frappe.session.user}, "name")
        if linked:
            return linked

    existing = _find_customer_by_email(email)
    if existing:
        return existing

    return _create_customer(customer_name, email, phone)


def _find_customer_by_email(email: str) -> "str | None":
    result = frappe.db.sql(
        """
        SELECT dl.link_name
        FROM   `tabContact`      c
        JOIN   `tabDynamic Link` dl
               ON  dl.parent       = c.name
               AND dl.parenttype   = 'Contact'
               AND dl.link_doctype = 'Customer'
        WHERE  c.email_id = %s
        LIMIT  1
        """,
        email,
    )
    return result[0][0] if result else None


def _create_customer(customer_name: str, email: str, phone: str = None) -> str:
    customer_group = (
        frappe.db.get_single_value("Selling Settings", "customer_group")
        or frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
        or "All Customer Groups"
    )
    territory = (
        frappe.db.get_single_value("Selling Settings", "territory")
        or frappe.db.get_value("Territory", {"is_group": 0}, "name")
        or "All Territories"
    )

    customer_doc = frappe.get_doc({
        "doctype": "Customer",
        "customer_name": customer_name,
        "customer_type": "Individual",
        "customer_group": customer_group,
        "email_id": email,
        "territory": territory,
    })
    customer_doc.insert(ignore_permissions=True)

    contact_data = {
        "doctype": "Contact",
        "first_name": customer_name,
        "email_id": email,
        "email_ids": [{"doctype": "Contact Email", "email_id": email, "is_primary": 1}],
        "links": [{
            "doctype": "Dynamic Link",
            "link_doctype": "Customer",
            "link_name": customer_doc.name,
        }],
    }
    if phone:
        contact_data["phone"] = phone
        contact_data["phone_nos"] = [
            {"doctype": "Contact Phone", "phone": phone, "is_primary_phone": 1}
        ]

    frappe.get_doc(contact_data).insert(ignore_permissions=True)
    return customer_doc.name


# ---------------------------------------------------------------------------
# Address
# ---------------------------------------------------------------------------

def _upsert_address(
    customer: str,
    addr: dict,
    addr_type: str,
    email: str = None,
    phone: str = None,
) -> str:
    address_line1 = addr.get("address_line1", "")
    city          = addr.get("city", "")

    existing = frappe.db.sql(
        """
        SELECT a.name
        FROM   `tabAddress`      a
        JOIN   `tabDynamic Link` dl
               ON  dl.parent       = a.name
               AND dl.parenttype   = 'Address'
               AND dl.link_doctype = 'Customer'
               AND dl.link_name    = %s
        WHERE  a.address_line1 = %s
               AND a.city = %s
        LIMIT  1
        """,
        (customer, address_line1, city),
    )

    if existing:
        address_doc = frappe.get_doc("Address", existing[0][0])
        _apply_address_fields(address_doc, addr, addr_type, email, phone)
        address_doc.save(ignore_permissions=True)
        return address_doc.name

    address_doc = frappe.new_doc("Address")
    address_doc.address_title = f"{customer}-{addr_type}"
    address_doc.append("links", {"link_doctype": "Customer", "link_name": customer})
    _apply_address_fields(address_doc, addr, addr_type, email, phone)
    address_doc.insert(ignore_permissions=True)
    return address_doc.name


def _apply_address_fields(
    address_doc, addr: dict, addr_type: str, email: str = None, phone: str = None
) -> None:
    address_doc.address_type        = addr_type
    address_doc.address_line1       = addr.get("address_line1", "")
    address_doc.address_line2       = addr.get("address_line2", "")
    address_doc.city                = addr.get("city", "")
    address_doc.state               = addr.get("state", "")
    address_doc.pincode             = addr.get("pincode", "")
    address_doc.country             = addr.get("country", "United States")
    address_doc.is_primary_address  = 1 if addr_type == "Billing"  else 0
    address_doc.is_shipping_address = 1 if addr_type == "Shipping" else 0
    if email:
        address_doc.email_id = email
    if phone:
        address_doc.phone = phone


# ---------------------------------------------------------------------------
# Invoice builder
# ---------------------------------------------------------------------------

def _get_default_company() -> str:
    company = frappe.defaults.get_global_default("company")
    if not company:
        company = frappe.db.get_value("Company", {}, "name")
    if not company:
        frappe.throw(
            _("No default company configured. Please set up a default company."),
            frappe.ValidationError,
        )
    return company


def _get_tax_template_for_state(state: str) -> "str | None":
    candidate = f"{state.strip()} - THB"
    return candidate if frappe.db.exists("Sales Taxes and Charges Template", candidate) else None


def _build_invoice(
    customer: str,
    company: str,
    items: list,
    billing_address: str = None,
    shipping_addr_name: str = None,
    notes: str = None,
    tax_template: str = None,
) -> "frappe.model.document.Document":
    selling_price_list = (
        frappe.db.get_single_value("Selling Settings", "selling_price_list")
        or frappe.db.get_value("Price List", {"selling": 1, "enabled": 1}, "name")
        or "Standard Selling"
    )

    invoice_items = []
    for row in items:
        item_meta = frappe.db.get_value(
            "Item",
            row["item_code"],
            ["item_name", "description", "stock_uom"],
            as_dict=True,
        )
        item_row = {
            "item_code":   row["item_code"],
            "item_name":   item_meta.item_name,
            "description": item_meta.description or item_meta.item_name,
            "qty":         flt(row["qty"]),
            "uom":         row.get("uom") or item_meta.stock_uom,
        }
        if row.get("rate"):
            item_row["rate"] = flt(row["rate"])
        invoice_items.append(item_row)

    doc_data = {
        "doctype":            "Sales Invoice",
        "naming_series":      "ACC-SINV-.YYYY.-",
        "customer":           customer,
        "company":            company,
        "posting_date":       nowdate(),
        "due_date":           nowdate(),
        "selling_price_list": selling_price_list,
        "update_stock":       0,
        "items":              invoice_items,
    }
    if billing_address:
        doc_data["customer_address"] = billing_address
    if shipping_addr_name:
        doc_data["shipping_address_name"] = shipping_addr_name
    if notes:
        doc_data["terms"] = notes
    if tax_template:
        doc_data["taxes_and_charges"] = tax_template

    invoice = frappe.get_doc(doc_data)
    invoice.set_missing_values()
    invoice.calculate_taxes_and_totals()
    return invoice


# ---------------------------------------------------------------------------
# Payment Entry
# ---------------------------------------------------------------------------

def _create_payment_entry(
    invoice: "frappe.model.document.Document",
    transaction_id: str,
) -> str:
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    _ensure_authorize_mode_of_payment()

    # Read outstanding_amount from DB after submit — avoids floating-point
    # mismatch between the in-memory grand_total and what ERPNext stored.
    outstanding = flt(frappe.db.get_value("Sales Invoice", invoice.name, "outstanding_amount"))

    pe = get_payment_entry("Sales Invoice", invoice.name, party_amount=outstanding)
    pe.mode_of_payment = "Authorize.net"
    pe.reference_no    = transaction_id
    pe.reference_date  = nowdate()
    pe.remarks         = f"Authorize.net transaction {transaction_id} for {invoice.name}"
    pe.insert(ignore_permissions=True)
    pe.submit()

    return pe.name


def _ensure_authorize_mode_of_payment():
    if not frappe.db.exists("Mode of Payment", "Authorize.net"):
        frappe.get_doc({
            "doctype":         "Mode of Payment",
            "mode_of_payment": "Authorize.net",
            "type":            "General",
        }).insert(ignore_permissions=True)


# ---------------------------------------------------------------------------
# Billing address mapper
# ---------------------------------------------------------------------------

def _map_billing(
    billing_address: "dict | None",
    customer_name: str,
    email: str,
    phone: str = None,
) -> "dict | None":
    if not billing_address:
        return None

    name_parts = customer_name.strip().split(" ", 1)
    return {
        "first_name": name_parts[0],
        "last_name":  name_parts[1] if len(name_parts) > 1 else "",
        "address":    billing_address.get("address_line1", ""),
        "city":       billing_address.get("city", ""),
        "state":      billing_address.get("state", ""),
        "zip":        billing_address.get("pincode", ""),
        "country":    billing_address.get("country", "US"),
    }


# ---------------------------------------------------------------------------
# Response serializer
# ---------------------------------------------------------------------------

def _serialize_invoice(doc) -> dict:
    return {
        "name":                    doc.name,
        "customer":                doc.customer,
        "customer_name":           doc.customer_name,
        "status":                  doc.status,
        "posting_date":            str(doc.posting_date),
        "due_date":                str(doc.due_date),
        "currency":                doc.currency,
        "selling_price_list":      doc.selling_price_list,
        "total":                   flt(doc.total),
        "net_total":               flt(doc.net_total),
        "total_taxes_and_charges": flt(doc.total_taxes_and_charges),
        "grand_total":             flt(doc.grand_total),
        "outstanding_amount":      flt(doc.outstanding_amount),
        "items": [
            {
                "item_code": row.item_code,
                "image":     row.image,
                "item_name": row.item_name,
                "qty":       flt(row.qty),
                "uom":       row.uom,
                "rate":      flt(row.rate),
                "amount":    flt(row.amount),
            }
            for row in doc.items
        ],
    }


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _parse_json(value, field_name: str):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            frappe.throw(
                _("Invalid JSON for field '{0}'").format(field_name),
                frappe.ValidationError,
            )
    return value
