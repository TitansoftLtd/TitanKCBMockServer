"""Page context for /kcb-account: a simulated bank account screen (demo only)."""

import re

import frappe

no_cache = 1


def get_context(context):
	account = (frappe.form_dict.get("account") or "").strip()
	context.account = account if re.fullmatch(r"\d{4,12}", account) else ""
	context.title = "KCB Internet Banking (demo)"
	return context
