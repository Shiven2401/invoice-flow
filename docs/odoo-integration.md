# Odoo integration

Odoo is the ERP backbone: purchase orders, receipts (Inventory), vendor bills, vendors and products live there.
Invoice Flow reads invoices, checks them against Odoo (a 3-way match: PO, receipt, invoice), and writes its decisions
and the purchasing changes people make in the app back to Odoo. Both directions are kept in step automatically.

## What happens where

| | Invoice Flow → Odoo | Odoo → Invoice Flow |
|---|---|---|
| **Invoices** | An approved invoice becomes a **draft vendor bill**, each line linked to its PO line (`purchase_line_id`), with the original PDF attached and an audit note (who, decision, why). A credit note becomes a draft **vendor credit note** once a person approves it. Held and rejected invoices get the note and PDF on their PO (or vendor). Never two bills for the same vendor and invoice number | The bill's state comes back: Draft, Posted, In payment, Paid |
| **Purchase orders** | Created in the app → created in Odoo as an RFQ (origin `Invoice Flow #<app id>`). Approved in the app → Odoo's **Confirm Order** (Odoo's own approval rule still applies: with two-step approval a large PO stays "To Approve" for a purchase manager). Changed in the app (vendor reference, expected date, lines) → written to Odoo | New POs, changes, approval and order states, receipt status, received and billed quantities |
| **Inventory** | – | Products with stock on hand, incoming and forecast; receipts (GRNs) and their moves; warehouses. The invoice checks use the **received** quantity: 100 ordered, 80 received → only 80 can be billed now |
| **Vendors, products** | Created in the app → created in Odoo (a GSTIN, reference or name Odoo already has is never added twice) | New vendors and products, and changes to them |
| **Users** | Each connected account becomes a contact tagged **Invoice Flow User** | – |

## How the sync works (app/erp_sync.py)

- **Mirror.** One table (`erp_records`) holds Odoo's POs (with lines), vendors, products (with stock), receipts (with
  moves) and vendor bills. Each row has the app's own id and the Odoo id, the Odoo reference, Odoo's `write_date`, when it
  was last synced and last synced successfully, its sync status (`synced`, `pending`, `error`, `conflict`, `deleted`), the
  last error and where the latest change came from (`odoo` or `app`). Invoice runs keep their bill id (`odoo_bill_id`),
  its state (`odoo_status`) and every note they left (`odoo_trail`).
- **Odoo → app**, three ways: **webhooks** from Odoo automation rules (instant; set up with `tools/odoo_webhooks.py`);
  an **incremental pull every minute** by `write_date`, which also picks up what a change touches without its own
  `write_date` moving (a receipt changes a PO line's received quantity and a product's stock; a payment changes a bill);
  and a **full reconcile every 15 minutes**, which also notices records deleted or archived in Odoo. The webhook body
  is never trusted: it only says which record to read from Odoo.
- **App → Odoo** goes through a **queue** (`erp_outbox`), kept after each write as the audit trail of what the app
  changed in Odoo and who asked. A write is tried at once. If Odoo is unreachable it waits and is retried with
  exponential backoff (15 s, doubling, at most 15 min) until Odoo answers; a refusal is retried three times and then
  waits for a person (Retry on the Sync tab). Nothing is dropped, and pending writes survive a restart.
- **Idempotent.** Every handler looks before it creates: a PO by its origin, a vendor by GSTIN or name, a product by
  reference or name, a bill by vendor and number. A write whose answer was lost (timeout after Odoo did it) is retried
  without making a second record. API writes accept a `request_id`: the same id twice is one write.
- **Conflicts.** An edit made in the app carries the `write_date` it was based on. If Odoo changed the record since,
  the edit is refused as a conflict and the mirror shows Odoo's version. Pulls never write to Odoo, so a change can't
  loop between the systems.
- **Decisions use Odoo live.** The invoice checks read vendors, POs and receipts from Odoo directly, never from the
  mirror. If Odoo doesn't answer, the invoice is held ("ERP unreachable"), never approved, and the queue checks it
  again by itself once Odoo answers.
- Reads that hit a network blip are retried (1 s, 3 s); writes are not retried blindly (the queue does it safely).

## The Odoo ERP page

For a connected account, the **Odoo ERP** section has: **Overview** (invoices received, processed, pending, approved,
rejected; draft, posted, paid and unpaid bills; POs by state and receipt status; invoice and PO value, pending
payment, paid, outstanding to vendors, tax; matched, partly matched, mismatched, missing-PO, price and quantity
differences), with date range and vendor filters; **Purchase orders** (new, edit, approve, details with lines,
receipts, bills and invoices); **Inventory**; **Receipts**; **Vendors & products** (new vendor, new product); **Bills**;
and **Sync** (mirror freshness, the queue with Retry, pull now, full reconcile). Tables sort by any column and filter
by date, vendor, status and search. The Dashboard also shows an Odoo strip and an **In Odoo** column in History.

## API (app/erp_api.py)

All need a signed-in account that is connected to Odoo. Writes need an account in `ODOO_APPROVERS` (or any connected
account when it's not set).

| | |
|---|---|
| `GET /api/erp/status` | connection, mirror freshness, queue counts |
| `GET /api/erp/overview?date_from&date_to&vendor` | the dashboard figures |
| `GET /api/erp/purchase-orders?state&receipt&q` · `GET /api/erp/purchase-orders/{id}` | POs; one PO with lines, receipts, bills, invoices, queue |
| `POST /api/erp/purchase-orders` | `{vendor_id, lines:[{product_id, qty, price, tax_ids, discount}], partner_ref, date_planned, approve, request_id}` |
| `PATCH /api/erp/purchase-orders/{id}` | `{base_version, partner_ref, date_planned, vendor_id, lines:[{id, qty, price, delete} or a new line], request_id}` |
| `POST /api/erp/purchase-orders/{id}/approve` | Confirm Order in Odoo |
| `GET/POST /api/erp/vendors` · `GET/POST /api/erp/products` | list (`q`) and create |
| `GET /api/erp/inventory` · `GET /api/erp/receipts?state` · `GET /api/erp/bills?state&payment` | inventory, GRNs, bills |
| `GET /api/erp/options` | taxes, units, payment terms, warehouses (for forms) |
| `GET /api/erp/sync` · `POST /api/erp/sync {full}` · `POST /api/erp/sync/{op_id}/retry` | the queue; pull now; retry a write |
| `POST /api/odoo/webhook?token=…` | called by Odoo (no sign-in; the token, derived from the API key, is the secret) |

## Settings

| Variable | |
|---|---|
| `ODOO_URL`, `ODOO_DB` | the Odoo and its database |
| `ODOO_USER` | the login the API key belongs to (needed for XML-RPC) |
| `ODOO_API_KEY` | avatar > My Preferences > Account Security > New API Key. Never in code, a commit or the page |
| `ODOO_PROTOCOL` | `xmlrpc` (Odoo's external API: `authenticate`, then `execute_kw`) or `json2` (Odoo 19+'s JSON-2 API, the default). The same calls either way |
| `ODOO_ACCOUNTS` | the Invoice Flow accounts connected to Odoo, comma-separated (always set it on a public server) |
| `ODOO_APPROVERS` | the accounts allowed to create, change or approve POs and add vendors or products; empty = every connected account |

Live (a Compute Engine VM behind Cloud Run): URL, database, user, protocol, accounts and approvers come from the VM's
metadata; the API key comes from **Secret Manager** (secret `odoo-api-key`, read at boot with the VM's service account,
which has the Secret Accessor role on that secret only). The startup script logs which source it used, never the key.

## Odoo-side setup

- **Apps**: Purchase, Inventory, Invoicing/Accounting (installed), plus **Automation Rules** (`base_automation`) for
  the webhooks: `python tools/odoo_webhooks.py setup <site url>` installs it if needed and creates one rule per synced
  model ("Invoice Flow sync: …", trigger on create and update, action "Send Webhook Notification"). Re-run it after
  rotating the API key (the webhook token changes with it). Without the rules everything still syncs, within a minute.
- **The API user** needs Purchase, Inventory and Accounting rights (the admin has them).
- **Two-step PO approval** (Purchase settings) is respected: POs above the threshold wait for a purchase manager in Odoo.
- Test data: `python tools/odoo_test_data.py setup` (vendors and POs, received through Inventory; PO-2026-1004
  deliberately not received); `reset-bills` removes draft test bills.

## Tests

- `pytest tests/test_erp_sync.py tests/test_odoo.py`: against stand-in Odoos (no network): mirroring every model,
  POs made or changed in Odoo and received, webhooks (token, record read from Odoo), POs created once and approved,
  two-step approval left to Odoo, edits and conflicts, Odoo down (queued, then sent once, even when the first answer
  was lost), refusals retried then left for a person, vendors and products created once, approver rights, bills never
  duplicated, credit notes, an invoice held while Odoo was down checked again when it answers, the 3-way check, the
  code guide's cases, the 14 samples, every audit-trail phase, XML-RPC call mapping.
- `python tools/erp_e2e.py <url> <account> <password> [.env] [--outage <proxy>]`: the 14 scenarios of the integration
  brief against a real Odoo through a running Invoice Flow, each checked on the other side.
- `python tools/p2p_e2e.py <url> <account> <password>`: procure-to-pay: PO, receipt, invoice, bill, post, pay.
