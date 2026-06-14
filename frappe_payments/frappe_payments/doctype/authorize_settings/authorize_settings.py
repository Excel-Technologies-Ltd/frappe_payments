import frappe
from frappe.model.document import Document


class AuthorizeSettings(Document):
    def on_update(self):
        from frappe_payments.utils.payment_settings import invalidate_authorize_settings_cache
        invalidate_authorize_settings_cache()
