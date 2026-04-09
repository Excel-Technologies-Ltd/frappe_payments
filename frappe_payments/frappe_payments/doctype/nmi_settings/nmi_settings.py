import frappe
from frappe.model.document import Document


class NMISettings(Document):
    def on_update(self):
        from frappe_payments.utils.payment_settings import invalidate_settings_cache
        invalidate_settings_cache()
