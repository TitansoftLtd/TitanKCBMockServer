"""Settings for the KCB mock server (demo only, never talks to real KCB)."""

import frappe
from frappe.model.document import Document

CALLBACK_PATH = "/api/method/titan_kcb.api.callbacks.funds_transfer"


class KCBMockServerSettings(Document):
	def onload(self):
		self.set_onload("effective_callback_url", get_callback_url())


def get_callback_url() -> str:
	"""Callback URL: the setting, else this bench's own web server.

	The callback goes to the same bench, so we use ``host_name`` if configured, otherwise the
	loopback webserver port; ``send_callback`` adds ``X-Frappe-Site-Name`` so the request is
	routed to this site (multi-site bench safe) regardless of the Host header.
	"""
	configured = frappe.db.get_single_value("KCB Mock Server Settings", "callback_url")
	if configured:
		return configured
	base = frappe.conf.host_name or f"http://127.0.0.1:{frappe.conf.webserver_port or 8000}"
	return base.rstrip("/") + CALLBACK_PATH


@frappe.whitelist()
def reset_mock() -> str:
	"""Reset balances, clear mock transactions and SMS."""
	frappe.only_for("System Manager")
	from titan_kcb_mock_server.utils import reset_all

	reset_all()
	return "Mock reset."
