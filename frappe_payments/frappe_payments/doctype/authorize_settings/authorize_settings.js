frappe.ui.form.on("Authorize Settings", {
    refresh(frm) {
        frm.set_intro(
            frm.doc.enabled
                ? __("Authorize.net payment gateway is <b>active</b>.")
                : __("Authorize.net payment gateway is <b>disabled</b>. Check 'Enabled' to accept payments."),
            frm.doc.enabled ? "green" : "orange"
        );

        if (frm.doc.client_key) {
            frm.add_custom_button(__("Copy Client Key"), () => {
                frappe.utils.copy_to_clipboard(frm.doc.client_key);
                frappe.show_alert({ message: __("Client key copied to clipboard"), indicator: "green" });
            });
        }
    },

    environment(frm) {
        if (frm.doc.environment === "Production") {
            frappe.msgprint({
                title: __("Production Environment"),
                message: __("You are switching to <b>Production</b>. Real transactions will be processed. Make sure your API credentials are live credentials."),
                indicator: "orange",
            });
        }
    },
});
