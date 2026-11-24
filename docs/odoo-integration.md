# Odoo integration

Invoice Flow reads vendors, purchase orders and receipts from Odoo, runs a 3-way match (PO, receipt, invoice) on every
line, and writes its decisions back. Odoo stays the system of record; Invoice Flow decides.

| Phase | What happens | Where it shows in Odoo |
|---|---|---|
| 0. Core | An approved invoice becomes a **draft vendor bill**, each line linked to its PO line (`purchase_line_id`), so Odoo's own "billed" quantity moves | Invoicing > Vendors > Bills |
| 1. Users | On sign-up or first sign-in, each connected account becomes a contact (reference `invoice-flow:<account>`) | Contacts, tag **Invoice Flow User** |
| 2. Audit trail | The original PDF is attached to the bill, plus an internal note: who processed it, the decision and why | The bill's attachment preview and chatter |
| 3. Holds and rejections | No bill; the note and the PDF go in the chatter of the invoice's PO (or the vendor's contact if the PO isn't in Odoo). A person's later decision adds its own note | The PO's chatter, e.g. *"Invoice GFF-3102 rejected: ... 16% above the PO ... Decision recorded by asha"* |
| 4. Back from Odoo | Bill states are read back (on every Dashboard load, at most every 15 s, and every 60 s in the background) | Invoice Flow shows Draft, Posted, In payment or Paid |

The 3-way match (`rules.three_way_check`): billing beyond the PO quantity by more than 10% rejects; billing more than
was received (and not yet billed) holds with **"Goods not received"**; a rate more than 2% above the PO holds, more than
10% rejects. If Odoo doesn't answer, the invoice is held ("ERP unreachable") and never approved; checking it again
re-runs everything against Odoo. Notes never change a decision: if Odoo refuses one, the run says so.

On the Dashboard, a connected account sees a strip with its bills by state and links into Odoo (bills, purchase
orders, its own contact), an **In Odoo** column in History (the bill and its state, or the PO the note went on), and the
full record in each invoice's detail panel. PO and vendor names in the PO ledger open in Odoo.

## Settings

| Variable | |
|---|---|
| `ODOO_URL` | e.g. `https://invoice-flow.odoo.com` |
| `ODOO_DB` | the database name |
| `ODOO_USER` | the login the API key belongs to (needed for XML-RPC) |
| `ODOO_API_KEY` | avatar > My Preferences > Account Security > New API Key. Never in code or a commit |
| `ODOO_PROTOCOL` | `xmlrpc` (Odoo's classic external API: `authenticate`, then `execute_kw`) or `json2` (Odoo 19+'s JSON-2 API, the default). Both send the same calls |
| `ODOO_ACCOUNTS` | the Invoice Flow accounts that use Odoo, comma-separated. On a public server always set it, so nobody else's invoices reach your Odoo |

On the live server (a Compute Engine VM behind the Cloud Run front end) the first three, the protocol and the account
list come from the VM's metadata; the API key comes from **Secret Manager** (secret `odoo-api-key`), read at boot with the
VM's service account. The startup script logs which source it used, never the key.

## Checking it

- `pytest tests/test_odoo.py`: the 3-way check, the code guide's 5 cases, the 14 samples, every phase, Odoo down or
  refusing, and the XML-RPC call mapping, against a stand-in Odoo (no network).
- `python tools/odoo_test_data.py setup`: the test vendors and POs in a real Odoo (confirmed, goods received through
  Inventory receipts; PO-2026-1004's services deliberately not received). `reset-bills` removes draft test bills.
- `python tools/p2p_e2e.py <url> <account> <password>`: procure-to-pay end to end in a real Odoo: a PO, its receipt in
  Inventory, the vendor's invoice through Invoice Flow, the draft bill with the PDF, posting it and registering the
  payment, with Invoice Flow showing Posted and then Paid.
