import base64
import json
import pathlib
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from titan_kcb_mock_server import api, settlement
from titan_kcb_mock_server.utils import seed_account

ACCOUNT = "9990000001"


def basic(key, secret):
	return "Basic " + base64.b64encode(f"{key}:{secret}".encode()).decode()


def valid_body(**over):
	body = {
		"companyCode": "KE0010001",
		"transactionType": "MO",
		"debitAccountNumber": ACCOUNT,
		"creditAccountNumber": "254712345678",
		"debitAmount": 25000,
		"paymentDetails": "Invoice payment",
		"transactionReference": "REF000000001",
		"currency": "KES",
		"beneficiaryDetails": "JOHN DOE",
		"beneficiaryBankCode": "MPESA",
	}
	body.update(over)
	return body


class TestMockServer(IntegrationTestCase):
	def tearDown(self):
		for dt in ("Mock FT Transaction", "Mock FT Callback Attempt", "Mock M-Pesa SMS", "Mock M-Pesa Wallet", "Mock KCB Account"):
			frappe.db.delete(dt)
		frappe.db.commit()

	def setUp(self):
		conf = patch.dict(frappe.conf, {"kcb_mock_consumer_key": "test-key", "kcb_mock_consumer_secret": "test-secret"})
		conf.start()
		self.addCleanup(conf.stop)
		frappe.db.delete("Mock FT Transaction")
		frappe.db.delete("Mock M-Pesa SMS")
		frappe.db.delete("Mock M-Pesa Wallet")
		seed_account(ACCOUNT, "Test Account", 1_000_000)
		self.headers = {}
		patcher = patch.object(api, "get_gateway_authorization", lambda: self.headers.get("Authorization", ""))
		patcher.start()
		self.addCleanup(patcher.stop)

	def bearer(self):
		self.headers["Authorization"] = basic("test-key", "test-secret")
		resp = api.token()
		tok = json.loads(resp.get_data())["access_token"]
		self.headers["Authorization"] = f"Bearer {tok}"

	def post(self, body):
		frappe.local.request = SimpleNamespace(get_json=lambda silent=True: body)
		with patch("frappe.enqueue") as enq:
			resp = api.funds_transfer()
		return resp, enq

	# --- auth ---
	def test_token_bad_credentials(self):
		self.headers["Authorization"] = basic("x", "y")
		resp = api.token()
		self.assertEqual(resp.status_code, 401)

	def test_token_missing_header(self):
		self.assertEqual(api.token().status_code, 401)

	def test_token_ok_shape(self):
		self.headers["Authorization"] = basic("test-key", "test-secret")
		data = json.loads(api.token().get_data())
		self.assertEqual(data["token_type"], "Bearer")
		self.assertTrue(data["access_token"])

	def test_transfer_requires_bearer(self):
		resp, _ = self.post(valid_body())
		self.assertEqual(resp.status_code, 401)
		self.assertEqual(json.loads(resp.get_data())["fault"]["code"], 900901)
		self.headers["Authorization"] = "Bearer nope"
		resp, _ = self.post(valid_body())
		self.assertEqual(resp.status_code, 401)

	# --- validation ---
	def test_validation_errors_reply_statuscode_1_and_enqueue_nothing(self):
		self.bearer()
		cases = [
			valid_body(companyCode=""),
			valid_body(paymentDetails="x" * 36),
			valid_body(transactionReference="x" * 13),
			valid_body(transactionType="RT"),
			valid_body(beneficiaryBankCode="01"),
			valid_body(currency="USD"),
			valid_body(creditAccountNumber="0712345678"),
			valid_body(debitAmount=0),
			valid_body(debitAmount="abc"),
			valid_body(debitAccountNumber="0000000000"),
		]
		for body in cases:
			resp, enq = self.post(body)
			self.assertEqual(resp.status_code, 200)
			self.assertEqual(json.loads(resp.get_data())["header"]["statusCode"], "1", body)
			enq.assert_not_called()
		self.assertEqual(frappe.db.count("Mock FT Transaction"), 0)

	def test_accept_then_duplicate(self):
		self.bearer()
		resp, enq = self.post(valid_body())
		header = json.loads(resp.get_data())["header"]
		self.assertEqual(header["statusCode"], "0")
		self.assertTrue(header["retrievalRefNumber"].startswith("FT"))
		enq.assert_called_once()
		resp, _ = self.post(valid_body())
		self.assertEqual(json.loads(resp.get_data())["header"]["statusCode"], "1")

	# --- SMS gateway ---
	def test_sms_gateway_delivers_to_phone(self):
		resp = api.send_sms(receiver="254712345678", message="ERPNext: 123456 is your OTP", sender="ERPNext", api_key="test-key")
		self.assertEqual(resp.status_code, 200)
		row = frappe.get_all("Mock M-Pesa SMS", filters={"msisdn": "254712345678"}, fields=["sender", "text"])[0]
		self.assertEqual((row.sender, row.text), ("ERPNEXT", "ERPNext: 123456 is your OTP"))

	def test_sms_gateway_rejects_bad_key_and_bad_input(self):
		self.assertEqual(api.send_sms(receiver="254712345678", message="x", api_key="nope").status_code, 401)
		key = "test-key"
		self.assertEqual(api.send_sms(receiver="123", message="x", api_key=key).status_code, 400)
		self.assertEqual(api.send_sms(receiver="254712345678", message=" ", api_key=key).status_code, 400)
		self.assertEqual(frappe.db.count("Mock M-Pesa SMS"), 0)

	# --- settlement outcomes ---
	def make_txn(self, msisdn="254712345678", amount=25000, ref="REF000000001"):
		return frappe.get_doc(
			{
				"doctype": "Mock FT Transaction",
				"transaction_reference": ref,
				"status": "PENDING",
				"debit_account_number": ACCOUNT,
				"credit_account_number": msisdn,
				"amount": amount,
				"beneficiary_details": "JOHN DOE",
				"ft_reference": "FT1234567890",
				"merchant_id": "m-1",
			}
		).insert(ignore_permissions=True)

	def test_success_moves_money_and_sends_sms(self):
		txn = self.make_txn()
		settlement.apply_settlement(txn.name)
		txn.reload()
		self.assertEqual(txn.status, "SUCCESS")
		self.assertTrue(re.fullmatch(r"[A-Z0-9]{10}", txn.mpesa_transaction_id))
		self.assertEqual(frappe.db.get_value("Mock KCB Account", ACCOUNT, "balance"), 975000)
		self.assertEqual(frappe.db.get_value("Mock M-Pesa Wallet", "254712345678", "balance"), 25000)
		msgs = frappe.get_all("Mock M-Pesa SMS", filters={"msisdn": "254712345678"}, fields=["sender", "text"])
		by_sender = {m.sender: m.text for m in msgs}
		self.assertEqual(set(by_sender), {"KCB", "MPESA"})
		self.assertIn("Confirmed. You have received Ksh25,000.00 from KCB BANK", by_sender["MPESA"])
		self.assertIn("sent to your M-PESA", by_sender["KCB"])

	def test_unregistered_wallet_fails(self):
		txn = self.make_txn("254700000000")
		settlement.apply_settlement(txn.name)
		txn.reload()
		self.assertEqual((txn.status, txn.failure_reason), ("FAILED", "M-PESA wallet not registered"))
		self.assertEqual(frappe.db.get_value("Mock KCB Account", ACCOUNT, "balance"), 1_000_000)

	def test_service_unavailable_fails(self):
		txn = self.make_txn("254711111111")
		settlement.apply_settlement(txn.name)
		txn.reload()
		self.assertEqual((txn.status, txn.failure_reason), ("FAILED", "M-PESA service unavailable"))

	def test_insufficient_funds_fails(self):
		txn = self.make_txn(amount=2_000_000)
		settlement.apply_settlement(txn.name)
		txn.reload()
		self.assertEqual((txn.status, txn.failure_reason), ("FAILED", "Insufficient funds in debit account"))

	def test_settlement_is_not_repeated(self):
		txn = self.make_txn()
		settlement.apply_settlement(txn.name)
		settlement.apply_settlement(txn.name)
		self.assertEqual(frappe.db.get_value("Mock KCB Account", ACCOUNT, "balance"), 975000)

	# --- callback retries ---
	def test_callback_retries_and_records_attempts(self):
		txn = self.make_txn()
		settlement.apply_settlement(txn.name)
		bad = MagicMock(status_code=500, text="boom")
		ok = MagicMock(status_code=200, text='{"statusCode":"0"}')
		with patch.object(settlement.requests, "post", side_effect=[bad, ok]) as post, patch.object(
			settlement.time, "sleep"
		):
			self.assertTrue(settlement.send_callback(txn.name))
		self.assertEqual(post.call_count, 2)
		txn.reload()
		self.assertEqual([r.http_status for r in txn.callback_attempts], [500, 200])
		sent = post.call_args.kwargs["json"]
		self.assertEqual(sent["transactionStatus"], "SUCCESS")
		self.assertEqual(sent["transactionReference"], "REF000000001")
		self.assertIn("X-Frappe-Site-Name", post.call_args.kwargs["headers"])

	def test_callback_gives_up_after_three_attempts(self):
		txn = self.make_txn()
		settlement.apply_settlement(txn.name)
		import requests

		with patch.object(settlement.requests, "post", side_effect=requests.ConnectionError("down")) as post, patch.object(
			settlement.time, "sleep"
		):
			self.assertFalse(settlement.send_callback(txn.name))
		self.assertEqual(post.call_count, 3)
		txn.reload()
		self.assertEqual(len(txn.callback_attempts), 3)


class TestImportBoundary(IntegrationTestCase):
	FORBIDDEN = re.compile(r"^\s*(import|from)\s+(erpnext|titan_pay|titan_kcb)\b(?!_mock_server)", re.M)

	def test_no_forbidden_imports(self):
		root = pathlib.Path(__file__).resolve().parents[1]
		offenders = []
		for path in root.rglob("*.py"):
			if "tests" in path.parts:
				continue
			if self.FORBIDDEN.search(path.read_text()):
				offenders.append(str(path))
		self.assertEqual(offenders, [])
