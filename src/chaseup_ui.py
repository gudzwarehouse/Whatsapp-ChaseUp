"""Local web UI for the chase-up flow -- run `python src/chaseup_ui.py` and
open http://localhost:8111.

Shows the ledger and WhatsApp bridge status, with two buttons:
  Fetch new orders -> chaseup_sync.sync()
  Chase up         -> WhatsApp availability check + send due messages
Stdlib http.server only, bound to localhost.
"""
import json
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chaseup_ledger as cl
import chaseup_send as send_mod
import chaseup_sync as sync_mod

PORT = 8111

_action_lock = threading.Lock()

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WhatsApp Chase-Up</title>
<style>
  :root {
    --bg: #f6f7f9; --card: #fff; --text: #1d2330; --muted: #667085; --line: #e4e7ec;
    --accent: #1a7f5a; --accent-text: #fff; --warn: #b54708; --bad: #b42318;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #14171c; --card: #1c2027; --text: #e8eaee; --muted: #98a2b3; --line: #2c323c;
            --accent: #2fa67b; --accent-text: #08130e; --warn: #f79009; --bad: #f97066; }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
  main { max-width: 1100px; margin: 0 auto; padding: 20px 16px 48px; }
  header { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; justify-content: space-between; }
  h1 { font-size: 20px; margin: 0; }
  .pill { display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 999px;
          border: 1px solid var(--line); background: var(--card); font-size: 13px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--bad); }
  .dot.on { background: var(--accent); }
  .actions { display: flex; flex-wrap: wrap; gap: 10px; margin: 18px 0; }
  button { font: inherit; padding: 9px 16px; border-radius: 8px; border: 1px solid var(--line);
           background: var(--card); color: var(--text); cursor: pointer; }
  button.primary { background: var(--accent); color: var(--accent-text); border-color: var(--accent); font-weight: 600; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-bottom: 16px; }
  .stat { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 10px 14px; }
  .stat b { display: block; font-size: 22px; }
  .stat span { color: var(--muted); font-size: 12px; }
  #msg { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 10px 14px;
         margin-bottom: 16px; white-space: pre-wrap; display: none; }
  #msg.err { border-color: var(--bad); color: var(--bad); }
  .tablewrap { background: var(--card); border: 1px solid var(--line); border-radius: 10px; overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; min-width: 760px; }
  th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--line); white-space: nowrap; }
  th { color: var(--muted); font-weight: 600; font-size: 12px; }
  tr:last-child td { border-bottom: 0; }
  .st { font-weight: 600; }
  .st-Pending { color: var(--warn); } .st-Replied { color: var(--accent); }
  .st-Exhausted, .st-No\\ WhatsApp { color: var(--bad); } .st-Ignored { color: var(--muted); }
  .empty { padding: 28px; text-align: center; color: var(--muted); }
  #filter { font: inherit; padding: 7px 10px; border-radius: 8px; border: 1px solid var(--line);
            background: var(--card); color: var(--text); }
</style>
</head>
<body>
<main>
  <header>
    <h1>WhatsApp Chase-Up</h1>
    <span class="pill"><span class="dot" id="dot"></span><span id="wa">Checking WhatsApp...</span></span>
  </header>

  <div class="actions">
    <button class="primary" id="btn-chase">Chase up</button>
    <button id="btn-fetch">Fetch new orders</button>
    <button id="btn-refresh">Refresh</button>
    <select id="filter">
      <option value="">All statuses</option>
      <option>Pending</option><option>Replied</option><option>Exhausted</option>
      <option>No WhatsApp</option>
    </select>
  </div>

  <div class="stats" id="stats"></div>
  <div id="msg"></div>
  <div class="tablewrap"><table>
    <thead><tr id="head"></tr></thead>
    <tbody id="body"></tbody>
  </table><div class="empty" id="empty" style="display:none"></div></div>
</main>

<script>
const COLS = ["Order ID","Phone","Customer Name","Has WhatsApp","Attempt","Last Sent Date","Next Due Date","Status"];
let rows = [];
const $ = id => document.getElementById(id);
$("head").innerHTML = COLS.map(c => `<th>${c}</th>`).join("");

function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

function showMsg(text, isErr) {
  const el = $("msg");
  el.textContent = text; el.className = isErr ? "err" : ""; el.style.display = "block";
}

function render() {
  const f = $("filter").value;
  const shown = f ? rows.filter(r => r.Status === f) : rows;
  const counts = {};
  rows.forEach(r => counts[r.Status] = (counts[r.Status] || 0) + 1);
  $("stats").innerHTML = [["Total", rows.length], ["Pending", counts.Pending||0], ["Replied", counts.Replied||0],
    ["Exhausted", counts.Exhausted||0], ["No WhatsApp", counts["No WhatsApp"]||0]]
    .map(([k,v]) => `<div class="stat"><b>${v}</b><span>${k}</span></div>`).join("");
  $("body").innerHTML = shown.map(r => "<tr>" + COLS.map(c =>
    c === "Status" ? `<td class="st st-${esc(r[c])}">${esc(r[c])}</td>` : `<td>${esc(r[c])}</td>`).join("") + "</tr>").join("");
  $("empty").style.display = shown.length ? "none" : "block";
  $("empty").textContent = rows.length ? "No orders match this filter." : "No orders in the ledger yet. Try Fetch new orders.";
}

async function api(path, method) {
  const r = await fetch(path, {method: method || "GET"});
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

async function loadStatus() {
  try {
    const s = await api("/api/status");
    $("dot").className = "dot" + (s.connected ? " on" : "");
    $("wa").textContent = s.connected ? "WhatsApp connected" : "WhatsApp not connected";
  } catch (e) { $("wa").textContent = "Status unavailable"; }
}

async function loadLedger() {
  try {
    rows = (await api("/api/ledger")).rows.filter(r => r.Status !== "Ignored");
    render();
  } catch (e) { showMsg("Could not load ledger: " + e.message, true); }
}

async function run(btn, path, confirmText, summarize) {
  if (confirmText && !confirm(confirmText)) return;
  const all = document.querySelectorAll("button");
  all.forEach(b => b.disabled = true);
  const label = btn.textContent; btn.textContent = "Working...";
  try {
    showMsg(summarize(await api(path, "POST")), false);
  } catch (e) { showMsg(e.message, true); }
  btn.textContent = label; all.forEach(b => b.disabled = false);
  loadStatus(); loadLedger();
}

const list = a => a.length ? a.join(", ") : "none";

$("btn-fetch").onclick = () => run($("btn-fetch"), "/api/fetch", null, r =>
  `Added ${r.added.length} new order(s): ${list(r.added)}` +
  (r.lookup_failed.length ? `\\nLookup failures:\\n  ` + r.lookup_failed.join("\\n  ") : ""));

$("btn-chase").onclick = () => run($("btn-chase"), "/api/chaseup",
  "This sends WhatsApp messages to every customer who is due a chase-up. Continue?", r =>
  `WhatsApp checked: ${list(r.checked)}\\nNo WhatsApp: ${list(r.no_whatsapp)}\\nSent: ${list(r.sent)}\\nExhausted: ${list(r.exhausted)}` +
  (r.errors.length ? `\\nErrors:\\n  ` + r.errors.join("\\n  ") : ""));

$("btn-refresh").onclick = () => { loadStatus(); loadLedger(); };
$("filter").onchange = render;
loadStatus(); loadLedger();
setInterval(loadStatus, 15000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/status":
            self._json({"connected": send_mod._bridge_connected()})
        elif self.path == "/api/ledger":
            self._guarded(lambda: {"rows": cl.get_all_rows()})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/api/fetch":
            self._guarded(sync_mod.sync, exclusive=True)
        elif self.path == "/api/chaseup":
            self._guarded(self._chaseup, exclusive=True)
        else:
            self._json({"error": "not found"}, 404)

    @staticmethod
    def _chaseup() -> dict:
        if not send_mod._bridge_connected():
            raise RuntimeError("WhatsApp bridge not connected -- start whatsapp-service and scan the QR first.")
        wa = send_mod.check_whatsapp_availability()
        sent = send_mod.send_due_messages()
        return {
            "checked": wa["checked"], "no_whatsapp": wa["no_whatsapp"],
            "sent": sent["sent"], "exhausted": sent["exhausted"],
            "errors": wa["errors"] + sent["errors"],
        }

    def _guarded(self, fn, exclusive=False):
        if exclusive and not _action_lock.acquire(blocking=False):
            return self._json({"error": "Another action is already running."}, 409)
        try:
            self._json(fn())
        except Exception as exc:
            traceback.print_exc()
            self._json({"error": str(exc)}, 500)
        finally:
            if exclusive:
                _action_lock.release()

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Chase-up UI: http://localhost:{PORT}")
    server.serve_forever()
