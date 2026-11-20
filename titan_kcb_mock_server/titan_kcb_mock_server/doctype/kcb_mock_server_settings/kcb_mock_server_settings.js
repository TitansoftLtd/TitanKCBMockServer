frappe.ui.form.on("KCB Mock Server Settings", {
	refresh(frm) {
		const url = (frm.doc.__onload || {}).effective_callback_url;
		if (url) frm.set_intro(__("Effective callback URL: {0}", [url]), "blue");
	},
	reset_mock(frm) {
		frappe.confirm(__("Reset balances and delete all mock transactions and SMS?"), () => {
			frappe.call({
				method: "titan_kcb_mock_server.titan_kcb_mock_server.doctype.kcb_mock_server_settings.kcb_mock_server_settings.reset_mock",
				callback: (r) => frappe.show_alert({ message: r.message, indicator: "green" }),
			});
		});
	},
});
