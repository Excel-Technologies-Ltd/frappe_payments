"""
Coupon code validation and discount application for payment endpoints.
"""
import frappe
from frappe.utils import flt, getdate, today as frappe_today


def resolve_coupon(coupon_code: str, customer: str = None):
    """
    Validate the coupon code and return the Coupon Code doc dict.
    Raises ValidationError for invalid / expired / exhausted coupons.
    Returns None if coupon_code is blank.
    """
    if not coupon_code or not str(coupon_code).strip():
        return None

    code = str(coupon_code).strip().upper()

    doc = frappe.db.get_value(
        "Coupon Code",
        {"coupon_code": code},
        ["name", "coupon_name", "coupon_type", "customer", "pricing_rule",
         "valid_from", "valid_upto", "maximum_use", "used"],
        as_dict=True,
    )

    if not doc:
        frappe.throw(f"Coupon code '{code}' is not valid.", frappe.ValidationError)

    if doc.customer and customer and doc.customer != customer:
        frappe.throw("This coupon is not valid for the given customer.", frappe.ValidationError)

    today_date = getdate(frappe_today())
    if doc.valid_from and getdate(doc.valid_from) > today_date:
        frappe.throw(f"Coupon '{code}' is not yet valid (starts {doc.valid_from}).", frappe.ValidationError)
    if doc.valid_upto and getdate(doc.valid_upto) < today_date:
        frappe.throw(f"Coupon '{code}' expired on {doc.valid_upto}.", frappe.ValidationError)

    if doc.maximum_use and (doc.used or 0) >= doc.maximum_use:
        frappe.throw(f"Coupon '{code}' has reached its usage limit.", frappe.ValidationError)

    return doc


def apply_coupon_to_invoice(invoice, coupon_code: str, customer: str = None) -> dict | None:
    """
    Validate the coupon and apply its discount to an in-memory Sales Invoice.

    Sets invoice.additional_discount_percentage or invoice.discount_amount, then
    calls invoice.calculate_taxes_and_totals() so grand_total reflects the discount.

    Returns a discount_info dict on success, None if coupon_code is blank.
    Raises ValidationError for invalid coupons.
    """
    doc = resolve_coupon(coupon_code, customer)
    if not doc:
        return None

    discount_info = {
        "coupon_code": str(coupon_code).strip().upper(),
        "coupon_name": doc.coupon_name,
        "discount_percentage": None,
        "discount_amount": None,
        "apply_discount_on": None,
    }

    if doc.pricing_rule:
        rule = frappe.db.get_value(
            "Pricing Rule",
            doc.pricing_rule,
            ["price_or_product_discount", "rate_or_discount",
             "discount_percentage", "discount_amount", "apply_discount_on"],
            as_dict=True,
        )
        if rule and rule.price_or_product_discount == "Price":
            apply_on = rule.apply_discount_on or "Grand Total"
            invoice.apply_additional_discount_on = apply_on
            discount_info["apply_discount_on"] = apply_on

            if rule.rate_or_discount == "Discount Percentage" and flt(rule.discount_percentage):
                invoice.additional_discount_percentage = flt(rule.discount_percentage)
                discount_info["discount_percentage"] = flt(rule.discount_percentage)
            elif flt(rule.discount_amount):
                invoice.discount_amount = flt(rule.discount_amount)
                discount_info["discount_amount"] = flt(rule.discount_amount)

    invoice.calculate_taxes_and_totals()
    return discount_info
