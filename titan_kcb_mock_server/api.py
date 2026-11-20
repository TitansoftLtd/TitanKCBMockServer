"""Mock KCB Buni gateway endpoints (demo only).

Responses are raw KCB-shaped JSON (not Frappe's ``{"message": ...}`` wrapper) so that the
``titan_kcb`` client cannot tell this mock from the real gateway.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
from typing import Any

import frappe
from werkzeug.wrappers import Response

from titan_kcb_mock_server.utils import (
	get_gateway_authorization,
	get_gateway_credentials,
	new_ft_reference,
	new_message_id,
)

TOKEN_TTL_SECONDS = 3600
TOKEN_CACHE_PREFIX = "kcb_mock_token:"

# (field, required, max length). Limits from KCB's OpenAPI definition [CONFIRMED].
FIELD_LIMITS: dict[str, tuple[bool, int | None]] = {
	"companyCode": (True, 15),
	"transactionType": (True, 2),
	"debitAccountNumber": (True, 10),
	# Spec caps creditAccountNumber at 10 but M-PESA numbers are 12 characters.
	# Unresolved with KCB, so the mock accepts up to 12 for MO. TODO(verify): P4.
	"creditAccountNumber": (True, 12),
	"debitAmount": (True, None),
	"paymentDetails": (True, 35),
	"transactionReference": (True, 12),
	"currency": (True, 3),
	"beneficiaryDetails": (True, 35),
	"beneficiaryBankCode": (False, 20),
}
MSISDN_RE = re.compile(r"^254[17]\d{8}$")


def _json_response(body: dict, status: int = 200) -> Response:
	return Response(json.dumps(body), status=status, mimetype="application/json")


def _fault(status: int = 401) -> Response:
	# WSO2-style gateway fault. TODO(verify): exact text.
	return _json_response(
		{
			"fault": {
				"code": 900901,
				"message": "Invalid Credentials",
				"description": "Invalid Credentials. Make sure you have provided the correct security credentials",
			}
		},
		status,
	)


@frappe.whitelist(allow_guest=True, methods=["POST", "GET"])
def token() -> Response:
	"""OAuth2 client-credentials token endpoint (Basic auth checked against the mock settings)."""
	expected_key, expected_secret = get_gateway_credentials()
	header = get_gateway_authorization()
	ok = False
	if header.lower().startswith("basic "):
		try:
			key, _, secret = base64.b64decode(header[6:]).decode().partition(":")
			ok = bool(expected_key) and secrets.compare_digest(key, expected_key) and secrets.compare_digest(
				secret, expected_secret
			)
		except Exception:
			ok = False
	if not ok:
		return _json_response(
			{"error_description": "A Valid OAuth client could not be found for client_id", "error": "invalid_client"},
			401,
		)
	access_token = secrets.token_hex(16)
	frappe.cache.set_value(TOKEN_CACHE_PREFIX + access_token, 1, expires_in_sec=TOKEN_TTL_SECONDS)
	return _json_response(
		{"access_token": access_token, "scope": "default", "token_type": "Bearer", "expires_in": TOKEN_TTL_SECONDS}
	)


def _valid_bearer() -> bool:
	header = get_gateway_authorization()
	if not header.lower().startswith("bearer "):
		return False
	return bool(frappe.cache.get_value(TOKEN_CACHE_PREFIX + header[7:].strip()))


def _header_response(status_code: str, message: str, description: str, extra: dict | None = None) -> Response:
	header = {
		"messageID": new_message_id(),
		"statusCode": status_code,
		"statusMessage": message,
		"statusDescription": description,
	}
	header.update(extra or {})
	return _json_response({"header": header})


def validate_request(body: dict[str, Any]) -> str | None:
	"""Return an error description, or None if the request is acceptable."""
	for field, (required, max_len) in FIELD_LIMITS.items():
		value = body.get(field)
		if value in (None, ""):
			if required:
				return f"{field} is required"
			continue
		if field == "debitAmount":
			continue
		if not isinstance(value, str):
			return f"{field} must be a string"
		if max_len and len(value) > max_len:
			return f"{field} exceeds maximum length of {max_len}"
	try:
		amount = float(body.get("debitAmount"))
	except (TypeError, ValueError):
		return "debitAmount must be a number"
	if amount <= 0:
		return "debitAmount must be greater than 0"
	if body["transactionType"] != "MO":
		return "Only transactionType MO is supported by the mock"
	if body.get("beneficiaryBankCode") != "MPESA":
		return "beneficiaryBankCode must be MPESA for transactionType MO"  # TODO(verify): P4
	if body["currency"] != "KES":
		return "Only currency KES is supported"
	if not MSISDN_RE.match(body["creditAccountNumber"]):
		return "creditAccountNumber must be a valid M-PESA number (2547XXXXXXXX / 2541XXXXXXXX)"
	if not frappe.db.exists("Mock KCB Account", body["debitAccountNumber"]):
		return "Unknown debitAccountNumber"
	if frappe.db.exists("Mock FT Transaction", {"transaction_reference": body["transactionReference"]}):
		return "Duplicate transactionReference"
	return None


@frappe.whitelist(allow_guest=True, methods=["POST"])
def funds_transfer() -> Response:
	"""Funds Transfer endpoint. Replies ``statusCode "0"`` = accepted for processing, not paid."""
	if not _valid_bearer():
		return _fault(401)

	body = frappe.request.get_json(silent=True)
	if not isinstance(body, dict):
		return _header_response("1", "Failed", "Request body must be a JSON object")

	error = validate_request(body)
	if error:
		return _header_response("1", "Failed", error)

	ft_reference = new_ft_reference()
	merchant_id = new_message_id()
	message_id = new_message_id()
	txn = frappe.get_doc(
		{
			"doctype": "Mock FT Transaction",
			"transaction_reference": body["transactionReference"],
			"status": "PENDING",
			"company_code": body["companyCode"],
			"transaction_type": body["transactionType"],
			"debit_account_number": body["debitAccountNumber"],
			"credit_account_number": body["creditAccountNumber"],
			"beneficiary_bank_code": body.get("beneficiaryBankCode"),
			"beneficiary_details": body["beneficiaryDetails"],
			"payment_details": body["paymentDetails"],
			"currency": body["currency"],
			"amount": float(body["debitAmount"]),
			"message_id": message_id,
			"ft_reference": ft_reference,
			"merchant_id": merchant_id,
			"request_payload": json.dumps(body, indent=1),
		}
	).insert(ignore_permissions=True)

	frappe.enqueue(
		"titan_kcb_mock_server.settlement.settle_transaction",
		queue="default",
		transaction_name=txn.name,
		enqueue_after_commit=True,
	)
	return _json_response(
		{
			"header": {
				"messageID": message_id,
				"statusCode": "0",
				"statusMessage": "Success",
				"statusDescription": "Success",
				"retrievalRefNumber": ft_reference,
				"merchantID": merchant_id,
			}
		}
	)


# ---- Mock SMS gateway (generic HTTP SMS API, as used by Frappe's SMS Settings) ----


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def send_sms(receiver: str = "", message: str = "", sender: str = "ERPNEXT", api_key: str = "") -> Response:
	"""Pretend SMS provider: delivers ``message`` to the simulated phone of ``receiver``.

	Auth: ``api_key`` must equal the mock consumer key. Demo only: no real SMS is sent.
	"""
	expected_key = get_gateway_credentials()[0]
	if not expected_key or not secrets.compare_digest(api_key or "", expected_key):
		return _json_response({"status": "error", "message": "invalid api_key"}, 401)
	digits = re.sub(r"\D", "", receiver or "")
	if digits.startswith("0") and len(digits) == 10:
		digits = "254" + digits[1:]
	if not MSISDN_RE.match(digits) or not (message or "").strip():
		return _json_response({"status": "error", "message": "receiver and message are required"}, 400)
	frappe.get_doc(
		{
			"doctype": "Mock M-Pesa SMS",
			"msisdn": digits,
			"sender": re.sub(r"[^A-Z0-9]", "", (sender or "ERPNEXT").upper()) or "ERPNEXT",
			"text": message,
			"sent_at": frappe.utils.now_datetime(),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()  # Frappe discards writes made during GET requests; SMS gateways are often called with GET
	return _json_response({"status": "sent", "receiver": digits})


# ---- Phone page / admin panel data (guest-allowed: mock data only, demo use) ----


@frappe.whitelist(allow_guest=True)
def get_phone_state(msisdn: str, after: str | None = None) -> dict:
	"""Wallet balance and SMS for a number. ``after`` is the last seen SMS name (e.g. MSMS-00003)."""
	if not MSISDN_RE.match(msisdn or ""):
		return {"balance": 0, "messages": []}
	balance = frappe.db.get_value("Mock M-Pesa Wallet", msisdn, "balance") or 0
	messages = frappe.get_all(
		"Mock M-Pesa SMS",
		filters={"msisdn": msisdn},
		fields=["name", "sender", "text", "sent_at", "creation"],
		order_by="creation asc, name asc",
		limit=100,
	)
	return {"balance": balance, "messages": messages}


@frappe.whitelist(allow_guest=True)
def get_account_state(account_number: str | None = None) -> dict:
	"""Data for the mock internet-banking page: accounts plus recent transactions of one account."""
	accounts = frappe.get_all(
		"Mock KCB Account", fields=["account_number", "account_name", "balance"], order_by="creation asc"
	)
	selected = account_number if any(a.account_number == account_number for a in accounts) else (
		accounts[0].account_number if accounts else None
	)
	txns = []
	if selected:
		txns = frappe.get_all(
			"Mock FT Transaction",
			filters={"debit_account_number": selected},
			fields=[
				"name", "transaction_reference", "ft_reference", "credit_account_number", "beneficiary_details",
				"payment_details", "amount", "currency", "status", "failure_reason", "mpesa_transaction_id", "creation",
			],
			order_by="creation desc",
			limit=25,
		)
	return {"accounts": accounts, "selected": selected, "transactions": txns}
