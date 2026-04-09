frappe.ui.form.on("NMI Settings", {
    refresh(frm) {
        frm.set_intro(
            frm.doc.enabled
                ? __("NMI payment gateway is <b>active</b>.")
                : __("NMI payment gateway is <b>disabled</b>. Check 'Enabled' to accept payments."),
            frm.doc.enabled ? "green" : "orange"
        );

        if (frm.doc.tokenization_key) {
            frm.add_custom_button(__("Copy Tokenization Key"), () => {
                frappe.utils.copy_to_clipboard(frm.doc.tokenization_key);
                frappe.show_alert({ message: __("Tokenization key copied to clipboard"), indicator: "green" });
            });
        }
    },

    environment(frm) {
        if (frm.doc.environment === "Production") {
            frappe.msgprint({
                title: __("Production Environment"),
                message: __("You are switching to <b>Production</b>. Real transactions will be processed. Make sure your API key is a live key."),
                indicator: "orange",
            });
        }
    },
});
