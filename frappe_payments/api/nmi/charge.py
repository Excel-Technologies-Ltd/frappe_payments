"""
NMI charge endpoint — single API call for the full eCommerce payment flow.

This endpoint is the payment-aware replacement for:
    true_med.api.sales_invoice.create_invoice.create_invoice

It does everything create_invoice does (Customer resolution, Address upsert,
Sales Invoice creation) PLUS charges the card via NMI and creates the Payment
Entry — so the invoice is immediately Paid with zero outstanding amount.

Workflow
--------
1. Parse & validate inputs (items, email, payment_token).
2. Elevate to Administrator to run ERPNext operations:
   a. Resolve or create Customer (by email).
   b. Upsert Billing and Shipping Address records.
   c. Build Sales Invoice in memory → calculate grand_total (no DB write yet).
3. Charge grand_total via NMI REST API v5 using the Collect.js payment_token.
4. On NMI approval, back under Administrator:
   a. Insert + Submit the Sales Invoice.
   b. Create + Submit a Payment Entry (mode: NMI, ref: transaction_id).
5. Return structured success response.

Endpoint
--------
POST /api/method/frappe_payments.api.nmi.charge.charge
"""

import json

import frappe
from frappe import _
from frappe.utils import flt, nowdate

from frappe_payments.utils.error_handler import ErrorCode, throw_payment_error, success_response
from frappe_payments.utils import nmi_client


@frappe.whitelist(allow_guest=True)
def charge(
    customer_name: str,
    email: str,
    items: "list | str",
    payment_token: str,
    phone: str = None,
    billing_address: "dict | str" = None,
    shipping_address: "dict | str" = None,
    notes: str = None,
) -> dict:
    """
    Charge a card via NMI and create all ERPNext documents in one call.

    Works for both guest and authenticated users. Login is NOT required.
    For authenticated users the system links the invoice to their existing
    Customer record; for guests it creates (or reuses) a Customer matched
    by email address — identical behaviour to create_invoice.

    Request body (JSON or form-data):

        customer_name    str  required   Full name of the buyer
        email            str  required   Email — used to identify/create Customer
        items            list required   [{"item_code": "X", "qty": 2}, ...]
                                         Optional "rate" key overrides price list.
        payment_token    str  required   One-time token from Collect.js (frontend)
        phone            str  optional   Contact phone number
        billing_address  dict optional   {
                                           "address_line1": "123 Gulshan Ave",
                                           "address_line2": "",          (optional)
                                           "city":          "Dhaka",
                                           "state":         "Dhaka",
                                           "pincode":       "1212",
                                           "country":       "Bangladesh"
                                         }
        shipping_address dict optional   Same shape as billing_address.
                                         Defaults to billing_address when omitted.
        notes            str  optional   Order notes / delivery instructions.

    Example request:
        {
          "customer_name": "John Doe",
          "email": "azmin@excelbd.com",
          "phone": "+8801700000000",
          "payment_token": "<collect_js_token>",
          "items": [
            {"item_code": "Truemed Glucosamine Sulfate KCL with Chondroitin and MSM", "qty": 2},
            {"item_code": "LUTEIN with Zeaxanthin (20mg Lutein + 4mg Zeaxanthin)-100 mg-RED", "qty": 1}
          ],
          "billing_address": {
            "address_line1": "123 Gulshan Ave",
            "city": "Dhaka",
            "state": "Dhaka",
            "pincode": "1212",
            "country": "Bangladesh"
          },
          "shipping_address": {
            "address_line1": "123 Gulshan Ave",
            "city": "Dhaka",
            "state": "Dhaka",
            "pincode": "1212",
            "country": "Bangladesh"
          },
          "notes": "Please call before delivery"
        }

    Response:
        {
          "success": true,
          "message": "Payment successful",
          "data": {
            "transaction_id": "9876543210",
            "invoice": {
              "name": "ACC-SINV-2026-00001",
              "customer": "John Doe",
              "status": "Paid",
              "posting_date": "2026-04-09",
              "currency": "USD",
              "grand_total": 199.98,
              "outstanding_amount": 0.0,
              "items": [...]
            },
            "payment_entry": "ACC-PAY-2026-00001"
          }
        }
    """
    # --- Deserialise JSON strings sent over HTTP form-data ------------------
    items            = _parse_json(items, "items")
    billing_address  = _parse_json(billing_address,  "billing_address")  if billing_address  else None
    shipping_address = _parse_json(shipping_address, "shipping_address") if shipping_address else None

    # --- Basic validation (payment token, email format, items list shape) ---
    _validate_basic(customer_name, email, payment_token, items)

    # --- ERPNext operations under Administrator (permission bypass) ----------
    _original_user = frappe.session.user
    try:
        frappe.set_user("Administrator")

        # Validate that every item_code exists and is enabled
        _validate_items(items)

        customer = _get_or_create_customer(customer_name, email, phone)
        company  = _get_default_company()

        # Billing address → also used as default shipping when not provided
        billing_addr_name  = None
        shipping_addr_name = None
        if billing_address:
            billing_addr_name  = _upsert_address(customer, billing_address,  "Billing")
            shipping_addr_name = billing_addr_name          # default

        # Explicit shipping address overrides the default
        if shipping_address:
            shipping_addr_name = _upsert_address(customer, shipping_address, "Shipping")

        # Build invoice in memory to compute grand_total — no DB write yet
        invoice     = _build_invoice(
            customer=customer,
            company=company,
            items=items,
            billing_address=billing_addr_name,
            shipping_addr_name=shipping_addr_name,
            notes=notes,
        )
        grand_total = flt(invoice.grand_total)

    finally:
        frappe.set_user(_original_user)

    # --- Charge the card via NMI (runs as original user, no DB writes) ------
    transaction = nmi_client.charge(
        amount=grand_total,
        payment_token=payment_token,
        billing=_map_billing_for_nmi(billing_address, customer_name, email, phone),
        description=f"Order for {customer_name}",
        customer_email=email,
    )

    # --- Persist ERPNext documents now that payment is confirmed ------------
    _original_user = frappe.session.user
    try:
        frappe.set_user("Administrator")

        invoice.insert(ignore_permissions=True)
        invoice.submit()

        payment_entry_name = _create_payment_entry(
            invoice=invoice,
            transaction_id=transaction["transaction_id"],
            amount=grand_total,
        )

    except Exception as exc:
        # Card was charged but ERPNext document creation failed.
        # Log the transaction_id so the team can reconcile manually.
        frappe.log_error(
            title="NMI Post-Charge Document Error",
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

    return success_response(
        message=_("Payment successful"),
        data={
            "transaction_id": transaction["transaction_id"],
            "invoice":        _serialize_invoice(invoice),
            "payment_entry":  payment_entry_name,
        },
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_basic(customer_name: str, email: str, payment_token: str, items: list):
    """Validate inputs that don't need a DB connection."""
    if not customer_name:
        frappe.throw(_("customer_name is required"), frappe.MandatoryError)

    if not email or "@" not in email:
        frappe.throw(_("A valid email address is required"), frappe.ValidationError)

    if not payment_token:
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
    """Validate that every item_code exists and is enabled. Runs as Administrator."""
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
    """
    Return a Customer name for the order.

    Priority:
      1. Authenticated non-admin user → find Customer linked to their User account.
      2. Email match → find Customer via Contact → Dynamic Link lookup.
      3. No match → create a new Customer + Contact.
    """
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

def _upsert_address(customer: str, addr: dict, addr_type: str) -> str:
    """
    Return the Address name for this customer+type, creating it if needed.
    Reuses an existing record when address_line1 + city already match.
    """
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
        return existing[0][0]

    address_doc = frappe.get_doc({
        "doctype":            "Address",
        "address_title":      f"{customer}-{addr_type}",
        "address_type":       addr_type,
        "address_line1":      address_line1,
        "address_line2":      addr.get("address_line2", ""),
        "city":               city,
        "state":              addr.get("state", ""),
        "pincode":            addr.get("pincode", ""),
        "country":            addr.get("country", "United States"),
        "is_primary_address": 1 if addr_type == "Billing"  else 0,
        "is_shipping_address":1 if addr_type == "Shipping" else 0,
        "links": [{
            "doctype":      "Dynamic Link",
            "link_doctype": "Customer",
            "link_name":    customer,
        }],
    })
    address_doc.insert(ignore_permissions=True)
    return address_doc.name


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


def _build_invoice(
    customer: str,
    company: str,
    items: list,
    billing_address: str = None,
    shipping_addr_name: str = None,
    notes: str = None,
) -> "frappe.model.document.Document":
    """
    Construct a Sales Invoice Document and run ERPNext's standard value-fill
    and tax-calculation routines — identical to what create_invoice does.
    """
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

    invoice = frappe.get_doc(doc_data)
    # ERPNext fills income accounts, cost centre, currency, exchange rate,
    # applies pricing rules, and sums up all totals + taxes
    invoice.set_missing_values()
    invoice.calculate_taxes_and_totals()
    return invoice


# ---------------------------------------------------------------------------
# Payment Entry
# ---------------------------------------------------------------------------

def _create_payment_entry(
    invoice: "frappe.model.document.Document",
    transaction_id: str,
    amount: float,
) -> str:
    """
    Create and submit a Payment Entry that fully settles the invoice.
    Uses ERPNext's get_payment_entry() so accounts are auto-resolved.
    """
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    _ensure_nmi_mode_of_payment()

    pe = get_payment_entry("Sales Invoice", invoice.name, party_amount=amount)
    pe.mode_of_payment = "NMI"
    pe.reference_no    = transaction_id
    pe.reference_date  = nowdate()
    pe.remarks         = f"NMI transaction {transaction_id} for {invoice.name}"
    pe.insert(ignore_permissions=True)
    pe.submit()

    return pe.name


def _ensure_nmi_mode_of_payment():
    """Create the 'NMI' Mode of Payment if it doesn't already exist."""
    if not frappe.db.exists("Mode of Payment", "NMI"):
        frappe.get_doc({
            "doctype":         "Mode of Payment",
            "mode_of_payment": "NMI",
            "type":            "General",
        }).insert(ignore_permissions=True)


# ---------------------------------------------------------------------------
# NMI billing address mapper
# ---------------------------------------------------------------------------

def _map_billing_for_nmi(
    billing_address: "dict | None",
    customer_name: str,
    email: str,
    phone: str = None,
) -> "dict | None":
    """
    Map our billing_address dict to NMI's expected field names.
    nmi_client._build_payload will strip any empty-string values before
    sending, so we can pass everything and let the client clean up.
    """
    if not billing_address:
        return None

    name_parts = customer_name.strip().split(" ", 1)
    return {
        "first_name": name_parts[0],
        "last_name":  name_parts[1] if len(name_parts) > 1 else "",
        "address1":   billing_address.get("address_line1", ""),
        "city":       billing_address.get("city", ""),
        "state":      billing_address.get("state", ""),
        "postal":     billing_address.get("pincode", ""),
        "country":    billing_address.get("country", "US"),
        "phone":      phone or "",
        "email":      email,
    }


# ---------------------------------------------------------------------------
# Response serializer
# ---------------------------------------------------------------------------

def _serialize_invoice(doc) -> dict:
    return {
        "name":                   doc.name,
        "customer":               doc.customer,
        "customer_name":          doc.customer_name,
        "status":                 doc.status,
        "posting_date":           str(doc.posting_date),
        "due_date":               str(doc.due_date),
        "currency":               doc.currency,
        "selling_price_list":     doc.selling_price_list,
        "total":                  flt(doc.total),
        "net_total":              flt(doc.net_total),
        "total_taxes_and_charges":flt(doc.total_taxes_and_charges),
        "grand_total":            flt(doc.grand_total),
        "outstanding_amount":     flt(doc.outstanding_amount),
        "items": [
            {
                "item_code": row.item_code,
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
    """Deserialise a value that may have arrived as a JSON string over HTTP."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            frappe.throw(
                _("Invalid JSON for field '{0}'").format(field_name),
                frappe.ValidationError,
            )
    return value
