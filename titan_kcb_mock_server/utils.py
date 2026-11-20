"""Shared helpers for the mock server. Demo only: never moves real money."""

from __future__ import annotations

import random
import string
import uuid

import frappe


def settings():
	return frappe.get_cached_doc("KCB Mock Server Settings")


def get_gateway_credentials() -> tuple[str, str]:
	"""The key/secret clients must present to the mock gateway.

	Read from ``site_config.json`` (``kcb_mock_consumer_key`` / ``kcb_mock_consumer_secret``) so no
	credentials live in the repository; falls back to the optional fields in the settings doc.
	Empty strings mean "not configured", and authentication then always fails.
	"""
	key = frappe.conf.get("kcb_mock_consumer_key")
	secret = frappe.conf.get("kcb_mock_consumer_secret")
	if key and secret:
		return key, secret
	cfg = settings()
	return cfg.consumer_key or "", cfg.get_password("consumer_secret", raise_exception=False) or ""


def new_message_id() -> str:
	return str(uuid.uuid4())


def new_ft_reference() -> str:
	"""KCB-like FT reference, e.g. FT22006670X0."""
	return "FT" + "".join(random.choices(string.digits + string.ascii_uppercase, k=10))


def new_mpesa_receipt() -> str:
	"""10-character uppercase alphanumeric M-PESA receipt."""
	return "".join(random.choices(string.ascii_uppercase + string.digits, k=10))


def get_or_create_wallet(msisdn: str):
	if frappe.db.exists("Mock M-Pesa Wallet", msisdn):
		return frappe.get_doc("Mock M-Pesa Wallet", msisdn)
	return frappe.get_doc(
		{"doctype": "Mock M-Pesa Wallet", "msisdn": msisdn, "wallet_name": msisdn, "balance": 0}
	).insert(ignore_permissions=True)


def seed_account(account_number: str, account_name: str = "Mock KCB Account", balance: float | None = None):
	"""Create (or top up/reset) a Mock KCB Account. Used by the titan_pay demo helper."""
	if balance is None:
		balance = settings().starting_balance or 1_000_000
	if frappe.db.exists("Mock KCB Account", account_number):
		frappe.db.set_value("Mock KCB Account", account_number, "balance", balance)
		return account_number
	frappe.get_doc(
		{
			"doctype": "Mock KCB Account",
			"account_number": account_number,
			"account_name": account_name,
			"balance": balance,
		}
	).insert(ignore_permissions=True)
	return account_number


def reset_all():
	"""Reset account balances to the starting balance; clear transactions, wallets and SMS."""
	start = settings().starting_balance or 1_000_000
	frappe.db.delete("Mock FT Transaction")
	frappe.db.delete("Mock FT Callback Attempt")
	frappe.db.delete("Mock M-Pesa SMS")
	frappe.db.delete("Mock M-Pesa Wallet")
	frappe.db.sql("update `tabMock KCB Account` set balance=%s", (start,))
	frappe.db.commit()


GATEWAY_PATH_PREFIX = "/api/method/titan_kcb_mock_server.api."
GATEWAY_AUTH_ATTR = "mock_gateway_authorization"


def stash_gateway_authorization() -> None:
	"""``before_request`` hook: hide the gateway's Authorization header from Frappe's own auth.

	Only applies to ``/api/method/titan_kcb_mock_server.api.*``. The endpoints read the value via
	``get_gateway_authorization()``.
	"""
	request = getattr(frappe.local, "request", None)
	if not request or not request.path.startswith(GATEWAY_PATH_PREFIX):
		return
	value = request.environ.pop("HTTP_AUTHORIZATION", None)
	if value:
		setattr(frappe.local, GATEWAY_AUTH_ATTR, value)


def get_gateway_authorization() -> str:
	"""The Authorization header sent to a mock gateway endpoint ("" if none)."""
	return getattr(frappe.local, GATEWAY_AUTH_ATTR, None) or frappe.get_request_header("Authorization") or ""
