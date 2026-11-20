"""Settlement of mock transfers: simulated M-PESA B2C leg + FT callback to the consumer."""

from __future__ import annotations

import json
import time
import uuid

import frappe
import requests

from titan_kcb_mock_server.titan_kcb_mock_server.doctype.kcb_mock_server_settings.kcb_mock_server_settings import (
	get_callback_url,
)
from titan_kcb_mock_server.utils import get_or_create_wallet, new_mpesa_receipt, settings

SMS_SENDER = "KCB BANK"  # PENDING(P12): sender name shown in the SMS
CALLBACK_ATTEMPTS = 3
CALLBACK_BACKOFF_SECONDS = (2, 4)  # waits before attempts 2 and 3
CALLBACK_HTTP_TIMEOUT = 10

# TODO(verify): Daraja B2C ResultCode -> ResultDesc. Check https://developer.safaricom.co.ke
B2C_RESULT_CODES: dict[int, str] = {
	0: "The service request is processed successfully.",
	2040: "The receiver is not a registered M-PESA customer.",
	17: "System internal error.",
}

# Magic MSISDNs for the demo: msisdn -> (daraja code, FT transactionMessage)
FAILING_MSISDNS: dict[str, tuple[int, str]] = {
	"254700000000": (2040, "M-PESA wallet not registered"),
	"254711111111": (17, "M-PESA service unavailable"),
}


def simulate_mpesa_b2c(msisdn: str, amount: float, receiver_name: str = "") -> dict:
	"""Return a Daraja-B2C-shaped Result. No network calls."""
	# Simulates KCB's internal M-PESA B2C leg. titan_kcb never calls this or Safaricom Daraja; KCB handles it on its side.
	conv = "AG_" + uuid.uuid4().hex[:20]
	originator = str(uuid.uuid4())
	code = FAILING_MSISDNS.get(msisdn, (0, ""))[0]
	result: dict = {
		"ResultType": 0,
		"ResultCode": code,
		"ResultDesc": B2C_RESULT_CODES[code],
		"OriginatorConversationID": originator,
		"ConversationID": conv,
	}
	if code == 0:
		receipt = new_mpesa_receipt()
		result["TransactionID"] = receipt
		result["ResultParameters"] = {
			"ResultParameter": [
				{"Key": "TransactionAmount", "Value": amount},
				{"Key": "TransactionReceipt", "Value": receipt},
				{"Key": "ReceiverPartyPublicName", "Value": f"{msisdn} - {receiver_name}"},
				{
					"Key": "TransactionCompletedDateTime",
					"Value": frappe.utils.now_datetime().strftime("%d.%m.%Y %H:%M:%S"),
				},
				{"Key": "B2CRecipientIsRegisteredCustomer", "Value": "Y"},
			]
		}
	return {"Result": result}


def format_sms(receipt: str, amount: float, balance: float, when) -> str:
	hour = when.strftime("%I").lstrip("0") or "12"
	return (
		f"{receipt} Confirmed. You have received Ksh{amount:,.2f} from {SMS_SENDER} on "
		f"{when.strftime('%d/%m/%y')} at {hour}:{when.strftime('%M %p')}. "
		f"New M-PESA balance is Ksh{balance:,.2f}."
	)


def format_kcb_sms(ft_reference: str, amount: float, account_name: str, when) -> str:
	"""Approximation of a KCB-side notice (wording invented for the demo; KCB's real text is unknown)."""
	return (
		f"KCB: Ksh{amount:,.2f} has been sent to your M-PESA from {account_name} on "
		f"{when.strftime('%d/%m/%y')}. Ref {ft_reference}."
	)


def settle_transaction(transaction_name: str) -> None:
	"""Background job: wait the configured delay, settle, then call back."""
	delay = int(settings().callback_delay_seconds or 0)
	if delay:
		time.sleep(delay)
	apply_settlement(transaction_name)
	frappe.db.commit()
	send_callback(transaction_name)


def apply_settlement(transaction_name: str) -> None:
	txn = frappe.get_doc("Mock FT Transaction", transaction_name)
	if txn.status != "PENDING":
		return
	msisdn, amount = txn.credit_account_number, float(txn.amount)
	account = frappe.get_doc("Mock KCB Account", txn.debit_account_number)

	if amount > float(account.balance):
		txn.status, txn.failure_reason = "FAILED", "Insufficient funds in debit account"
	else:
		b2c = simulate_mpesa_b2c(msisdn, amount, txn.beneficiary_details)
		txn.b2c_result = json.dumps(b2c, indent=1)
		if b2c["Result"]["ResultCode"] != 0:
			txn.status = "FAILED"
			txn.failure_reason = FAILING_MSISDNS[msisdn][1]
		else:
			receipt = b2c["Result"]["TransactionID"]
			account.balance = float(account.balance) - amount
			account.save(ignore_permissions=True)
			wallet = get_or_create_wallet(msisdn)
			wallet.balance = float(wallet.balance) + amount
			wallet.save(ignore_permissions=True)
			now = frappe.utils.now_datetime()
			frappe.get_doc(
				{
					"doctype": "Mock M-Pesa SMS",
					"msisdn": msisdn,
					"sender": "KCB",
					"text": format_kcb_sms(txn.ft_reference, amount, account.account_name or "KCB account", now),
					"sent_at": now,
				}
			).insert(ignore_permissions=True)
			frappe.get_doc(
				{
					"doctype": "Mock M-Pesa SMS",
					"msisdn": msisdn,
					"sender": "MPESA",
					"text": format_sms(receipt, amount, float(wallet.balance), now),
					"sent_at": now,
				}
			).insert(ignore_permissions=True)
			txn.status, txn.mpesa_transaction_id = "SUCCESS", receipt
	txn.save(ignore_permissions=True)


def build_callback_payload(txn) -> dict:
	"""FT callback body. TODO(verify): replace with the official spec's version (P3)."""
	return {
		"ftReference": txn.ft_reference,
		"transactionDate": frappe.utils.now_datetime().strftime("%Y-%m-%dT%H:%M:%S"),
		"amount": float(txn.amount),
		"transactionStatus": txn.status,
		"transactionMessage": (
			"Transaction processed successfully" if txn.status == "SUCCESS" else txn.failure_reason
		),
		"beneficiaryAccountNumber": txn.credit_account_number,
		"beneficiaryName": txn.beneficiary_details,
		"transactionReference": txn.transaction_reference,
		"merchantId": txn.merchant_id,
		"debitAccountNumber": txn.debit_account_number,
	}


def send_callback(transaction_name: str) -> bool:
	"""POST the FT callback, up to 3 attempts with backoff, recording each attempt.

	The callback goes to the same bench; ``X-Frappe-Site-Name`` makes Frappe route the request to
	this site whatever host/port we dial.
	"""
	txn = frappe.get_doc("Mock FT Transaction", transaction_name)
	payload = build_callback_payload(txn)
	url = get_callback_url()
	headers = {
		"Content-Type": "application/json",
		"X-Frappe-Site-Name": settings().callback_site_header or frappe.local.site,
	}
	for attempt in range(1, CALLBACK_ATTEMPTS + 1):
		row = {"attempt_no": attempt, "attempted_at": frappe.utils.now_datetime()}
		try:
			resp = requests.post(url, json=payload, headers=headers, timeout=CALLBACK_HTTP_TIMEOUT)
			row.update(http_status=resp.status_code, response_body=resp.text[:500])
			delivered = 200 <= resp.status_code < 300
		except requests.RequestException as e:
			row.update(http_status=0, error=str(e)[:500])
			delivered = False
		txn.append("callback_attempts", row)
		txn.save(ignore_permissions=True)
		frappe.db.commit()
		if delivered:
			return True
		if attempt < CALLBACK_ATTEMPTS:
			time.sleep(CALLBACK_BACKOFF_SECONDS[attempt - 1])
	return False
