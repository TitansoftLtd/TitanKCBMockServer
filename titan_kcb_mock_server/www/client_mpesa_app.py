"""Page context for /client-mpesa-app: a simulated phone showing SMS threads (demo only)."""

import re

import frappe

no_cache = 1


def get_context(context):
	msisdn = (frappe.form_dict.get("msisdn") or "").strip()
	context.msisdn = msisdn if re.fullmatch(r"\d{9,12}", msisdn) else ""
	context.title = "Client M-Pesa Application"
	return context
