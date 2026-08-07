"""Local Turso dashboard — income, expenses, and net profit via HTTP (no libsql)."""

from __future__ import annotations

import os
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from flask import Flask, render_template_string

load_dotenv()

app = Flask(__name__)


def normalize_turso_url(raw: str) -> str:
    """Turn Turso libsql/https URLs into the base HTTPS origin for the HTTP API."""
    url = (raw or "").strip().strip("'\"")
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://") :]
    elif url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/").split("/v2/")[0]


TURSO_URL = normalize_turso_url(os.getenv("TURSO_DATABASE_URL", ""))
TURSO_TOKEN = (os.getenv("TURSO_AUTH_TOKEN") or "").strip().strip("'\"")


def execute_turso_sql(sql: str, args: list | None = None) -> list[tuple]:
    """Run a read query against Turso HTTP Pipeline API. Returns rows as tuples."""
    if args is None:
        args = []

    formatted_args = []
    for arg in args:
        if isinstance(arg, float):
            formatted_args.append({"type": "float", "value": arg})
        elif isinstance(arg, int):
            formatted_args.append({"type": "integer", "value": arg})
        elif arg is None:
            formatted_args.append({"type": "null"})
        else:
            formatted_args.append({"type": "text", "value": str(arg)})

    if not TURSO_URL or not TURSO_TOKEN:
        raise RuntimeError(
            "Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN in .env to load the dashboard."
        )

    payload = {
        "requests": [
            {"type": "execute", "stmt": {"sql": sql, "args": formatted_args}},
            {"type": "close"},
        ]
    }
    headers = {
        "Authorization": f"Bearer {TURSO_TOKEN}",
        "Content-Type": "application/json",
    }
    response = requests.post(
        f"{TURSO_URL}/v2/pipeline", json=payload, headers=headers, timeout=30
    )
    if not response.ok:
        host = urlparse(TURSO_URL).hostname or TURSO_URL
        raise RuntimeError(
            f"Turso API error ({response.status_code}) for {host}: {response.text[:300]}"
        )

    data = response.json()
    results = data["results"][0]
    if results.get("type") == "error":
        raise RuntimeError(results["error"]["message"])

    stmt_result = results["response"]["result"]
    rows: list[tuple] = []
    for row in stmt_result.get("rows", []):
        rows.append(tuple(col.get("value") for col in row))
    return rows


def _as_float(value) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def fetch_dashboard_stats() -> dict:
    """Pull earnings + receipts and compute income / expenses / net profit."""
    earnings_rows = execute_turso_sql(
        "SELECT id, date, gross_earnings, uber_fees, net_payout, tips, source "
        "FROM earnings ORDER BY date DESC, id DESC LIMIT 50;"
    )
    receipt_rows = execute_turso_sql(
        "SELECT id, merchant, date, amount, item, category "
        "FROM receipts ORDER BY id DESC LIMIT 50;"
    )

    income_agg = execute_turso_sql(
        "SELECT "
        "COALESCE(SUM(gross_earnings), 0), "
        "COALESCE(SUM(net_payout), 0), "
        "COALESCE(SUM(tips), 0), "
        "COALESCE(SUM(uber_fees), 0), "
        "COUNT(*) "
        "FROM earnings;"
    )
    expense_agg = execute_turso_sql(
        "SELECT COALESCE(SUM(amount), 0), COUNT(*) FROM receipts;"
    )

    gross_income = _as_float(income_agg[0][0]) if income_agg else 0.0
    net_payout_total = _as_float(income_agg[0][1]) if income_agg else 0.0
    tips_total = _as_float(income_agg[0][2]) if income_agg else 0.0
    fees_total = _as_float(income_agg[0][3]) if income_agg else 0.0
    earnings_count = int(float(income_agg[0][4])) if income_agg else 0

    total_expenses = _as_float(expense_agg[0][0]) if expense_agg else 0.0
    receipt_count = int(float(expense_agg[0][1])) if expense_agg else 0

    # Income = gross Uber earnings; expenses = receipt spend; profit = income - expenses.
    total_income = gross_income
    net_profit = total_income - total_expenses

    return {
        "total_income": total_income,
        "total_expenses": total_expenses,
        "net_profit": net_profit,
        "net_payout_total": net_payout_total,
        "tips_total": tips_total,
        "fees_total": fees_total,
        "earnings_count": earnings_count,
        "receipt_count": receipt_count,
        "earnings": earnings_rows,
        "receipts": receipt_rows,
        "error": None,
    }


DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Uber Tax Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,400;0,9..40,600;0,9..40,700;1,9..40,400&family=Fraunces:opsz,wght@9..144,600;9..144,700&display=swap" rel="stylesheet" />
  <style>
    :root {
      --ink: #1a1814;
      --muted: #5c564c;
      --paper: #f3efe6;
      --panel: rgba(255, 252, 245, 0.88);
      --line: rgba(26, 24, 20, 0.12);
      --income: #1f6b4a;
      --expense: #9a3412;
      --profit: #1d4e89;
      --accent: #c45c26;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: "DM Sans", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(1200px 600px at 10% -10%, #f6d7b0 0%, transparent 55%),
        radial-gradient(900px 500px at 100% 0%, #c8ddd0 0%, transparent 50%),
        linear-gradient(165deg, #efe8da 0%, #e4ddd0 45%, #d9e2e0 100%);
    }
    .wrap {
      max-width: 1100px;
      margin: 0 auto;
      padding: 2.5rem 1.25rem 3rem;
    }
    header {
      margin-bottom: 2rem;
    }
    header h1 {
      font-family: "Fraunces", serif;
      font-size: clamp(2rem, 4vw, 2.75rem);
      font-weight: 700;
      letter-spacing: -0.02em;
      margin: 0 0 0.35rem;
    }
    header p {
      margin: 0;
      color: var(--muted);
      font-size: 1.05rem;
    }
    .metrics {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 1rem;
      margin-bottom: 1.5rem;
    }
    @media (max-width: 720px) {
      .metrics { grid-template-columns: 1fr; }
    }
    .metric {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 1.25rem 1.35rem;
      backdrop-filter: blur(8px);
      box-shadow: 0 10px 30px rgba(26, 24, 20, 0.06);
    }
    .metric .label {
      font-size: 0.85rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--muted);
      font-weight: 600;
    }
    .metric .value {
      font-family: "Fraunces", serif;
      font-size: 2rem;
      margin-top: 0.35rem;
      font-weight: 700;
    }
    .metric.income .value { color: var(--income); }
    .metric.expense .value { color: var(--expense); }
    .metric.profit .value { color: var(--profit); }
    .metric.profit.neg .value { color: var(--expense); }
    .substats {
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem 1.25rem;
      margin-bottom: 2rem;
      color: var(--muted);
      font-size: 0.95rem;
    }
    .panels {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 1rem;
    }
    @media (max-width: 900px) {
      .panels { grid-template-columns: 1fr; }
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 1.1rem 1.2rem 0.5rem;
      overflow: auto;
    }
    .panel h2 {
      font-family: "Fraunces", serif;
      font-size: 1.25rem;
      margin: 0 0 0.85rem;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.9rem;
    }
    th, td {
      text-align: left;
      padding: 0.55rem 0.35rem;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-weight: 600;
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .empty, .error {
      padding: 1rem 0 1.25rem;
      color: var(--muted);
    }
    .error { color: var(--expense); }
    footer {
      margin-top: 2rem;
      color: var(--muted);
      font-size: 0.85rem;
    }
    a { color: var(--accent); }
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>Uber Tax Dashboard</h1>
      <p>Live totals from Turso — earnings as income, receipts as expenses.</p>
    </header>

    {% if error %}
      <p class="error">{{ error }}</p>
    {% else %}
      <section class="metrics">
        <article class="metric income">
          <div class="label">Total income</div>
          <div class="value">${{ "%.2f"|format(total_income) }}</div>
        </article>
        <article class="metric expense">
          <div class="label">Total expenses</div>
          <div class="value">${{ "%.2f"|format(total_expenses) }}</div>
        </article>
        <article class="metric profit {% if net_profit < 0 %}neg{% endif %}">
          <div class="label">Net profit</div>
          <div class="value">${{ "%.2f"|format(net_profit) }}</div>
        </article>
      </section>

      <div class="substats">
        <span>{{ earnings_count }} earning record(s)</span>
        <span>{{ receipt_count }} receipt(s)</span>
        <span>Net payouts ${{ "%.2f"|format(net_payout_total) }}</span>
        <span>Tips ${{ "%.2f"|format(tips_total) }}</span>
        <span>Uber fees ${{ "%.2f"|format(fees_total) }}</span>
      </div>

      <section class="panels">
        <div class="panel">
          <h2>Recent earnings</h2>
          {% if earnings %}
          <table>
            <thead>
              <tr>
                <th>Date</th>
                <th>Gross</th>
                <th>Net</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {% for row in earnings %}
              <tr>
                <td>{{ row[1] }}</td>
                <td>${{ "%.2f"|format(row[2]|float) }}</td>
                <td>${{ "%.2f"|format(row[4]|float) }}</td>
                <td>{{ row[6] or "—" }}</td>
              </tr>
              {% endfor %}
            </tbody>
          </table>
          {% else %}
          <p class="empty">No earnings rows yet. The bot will create the table on startup; add payouts when ready.</p>
          {% endif %}
        </div>

        <div class="panel">
          <h2>Recent receipts</h2>
          {% if receipts %}
          <table>
            <thead>
              <tr>
                <th>Date</th>
                <th>Merchant</th>
                <th>Amount</th>
                <th>Category</th>
              </tr>
            </thead>
            <tbody>
              {% for row in receipts %}
              <tr>
                <td>{{ row[2] }}</td>
                <td>{{ row[1] }}</td>
                <td>${{ "%.2f"|format(row[3]|float) }}</td>
                <td>{{ row[5] or "—" }}</td>
              </tr>
              {% endfor %}
            </tbody>
          </table>
          {% else %}
          <p class="empty">No receipts in Turso yet.</p>
          {% endif %}
        </div>
      </section>
    {% endif %}

    <footer>
      Connected via Turso HTTP API (no libsql). Run locally with
      <code>python app.py</code>.
    </footer>
  </div>
</body>
</html>
"""


@app.route("/")
def dashboard():
    try:
        stats = fetch_dashboard_stats()
    except Exception as exc:
        stats = {
            "total_income": 0.0,
            "total_expenses": 0.0,
            "net_profit": 0.0,
            "net_payout_total": 0.0,
            "tips_total": 0.0,
            "fees_total": 0.0,
            "earnings_count": 0,
            "receipt_count": 0,
            "earnings": [],
            "receipts": [],
            "error": str(exc),
        }
    return render_template_string(DASHBOARD_HTML, **stats)


if __name__ == "__main__":
    port = int(os.environ.get("DASHBOARD_PORT", os.environ.get("PORT", 5000)))
    app.run(host="127.0.0.1", port=port, debug=True)
