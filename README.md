# titan_kcb_mock_server: stand-in for KCB Buni (demo only)

Simulates KCB's token and Funds Transfer endpoints and the M-PESA B2C leg, then calls back
`titan_kcb`. It **never moves real money and never calls an external API**. It depends on `frappe`
only and talks to the other apps over HTTP only.

## Endpoints (KCB-shaped raw JSON, no `{"message": …}` wrapper)
- `POST /api/method/titan_kcb_mock_server.api.token`: Basic auth checked against `kcb_mock_consumer_key` / `kcb_mock_consumer_secret` in `site_config.json` (no credentials are stored in the repository).
- `POST /api/method/titan_kcb_mock_server.api.funds_transfer`: Bearer token required (else 401 gateway fault). Validation errors reply `statusCode "1"` with no callback; otherwise `statusCode "0"` and settlement is queued.
- `POST|GET /api/method/titan_kcb_mock_server.api.send_sms` (`receiver`, `message`, `sender`, `api_key`): a pretend SMS provider used for ERPNext's approval OTPs; the SMS shows up in the phone's ERPNext thread.
- Phone page data: `get_phone_state`, `get_account_state` (guest-allowed, mock data only).

## Settlement rules
| Credit number / case | Result |
|---|---|
| `254700000000` | FAILED "M-PESA wallet not registered" |
| `254711111111` | FAILED "M-PESA service unavailable" |
| amount > mock KCB balance | FAILED "Insufficient funds in debit account" |
| anything else | SUCCESS: debit account, credit wallet, SMS |

The FT callback is POSTed to *Callback URL* (3 attempts, 2 s and 4 s backoff, each attempt logged on the Mock FT Transaction).

**Reaching the right site:** the callback goes to the same bench (`host_name`, else `http://127.0.0.1:<webserver_port>`) with an `X-Frappe-Site-Name: <site>` header, which makes Frappe serve it from this site. Override in the settings if needed.

**Why a `before_request` hook:** Frappe core answers 401 to any `Authorization: Basic/Bearer` header before an endpoint runs. For paths under `/api/method/titan_kcb_mock_server.api.` the hook moves that header aside so the mock can validate it like KCB would.

## Client M-Pesa Application
`/client-mpesa-app?msisdn=2547…` shows only a phone mockup (390×844 screen) with a Messages list and three threads: **ERPNext** (approval OTPs; tap the highlighted code to copy it), **MPESA** (with the wallet balance) and **KCB BANK**. Tap a thread to open it; new SMS slide in with a notification banner, an unread badge and a tone. `#erpnext` / `#mpesa` / `#kcb` after the URL opens a thread directly. The page **polls every 2 seconds** (guest realtime is not used).

Sound is on by default, but browsers only allow audio after a user gesture: the first click, tap or key press on the page unlocks it.

## KCB account page
`/kcb-account` (optional `?account=1234567890`) is a simulated internet-banking screen for the mock KCB account: balance, total sent to M-PESA and a mini statement with a running balance. It is a look-alike for the demo, not KCB's real design, and it has no login. It refreshes every 2 seconds.

## Settings
*KCB Mock Server Settings*: callback URL, callback delay (default 3 s), optional key/secret override, starting balance and a **Reset mock** button.

## Tests
`bench --site <site> run-tests --app titan_kcb_mock_server` (these tests delete mock data).
