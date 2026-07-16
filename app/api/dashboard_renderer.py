from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from app.dashboard import DashboardView, RoutingShare
from app.repositories.requests_repo import BreakdownRow, RequestListRow


def render_dashboard(view: DashboardView) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Gateway dashboard</title>
  <style>{_CSS}</style>
</head>
<body>
<main>
  <header>
    <div><p class="eyebrow">LLM cost-aware routing</p><h1>Gateway dashboard</h1></div>
    <p class="muted">Updated {_timestamp(int(view.generated_at.timestamp()))} · UTC calendar periods</p>
  </header>
  <section class="cards" aria-label="Spend summary">
    {_metric("Today", _money(view.spend.today))}
    {_metric("This week", _money(view.spend.week))}
    {_metric("This month", _money(view.spend.month))}
    {_budget(view)}
  </section>
  <p class="muted">Spend totals use the request ledger and exclude internal classifier probe calls, which are not currently recorded.</p>
  <section class="panel">
    <h2>Savings vs single-model baseline</h2>
    {_baseline(view)}
  </section>
  <section>
    <h2>Monthly spend breakdown</h2>
    <div class="grid-3">
      {_breakdown("By pool", view.by_pool)}
      {_breakdown("By model", view.by_model)}
      {_breakdown("By provider", view.by_provider)}
    </div>
  </section>
  <section>
    <h2>Routing mix</h2>
    <div class="grid-2">
      {_routing("By pool", view.routing_by_pool)}
      {_routing("By route stage", view.routing_by_stage)}
    </div>
  </section>
  <section class="panel">
    <h2>Recent requests</h2>
    {_recent(view.recent)}
  </section>
  <section class="panel">
    <div class="section-heading"><h2>Fallback events</h2><p class="muted">Events are derived from rows with <code>fallback_from</code>. Cooldown trips are not separately recorded in the current request ledger.</p></div>
    {_fallbacks(view.fallbacks)}
  </section>
</main>
</body>
</html>"""


def _metric(label: str, value: str) -> str:
    return f'<article class="metric"><p>{escape(label)}</p><strong>{value}</strong></article>'


def _budget(view: DashboardView) -> str:
    if view.budget_usd is None or view.budget_percent is None:
        return _metric("Monthly budget", "Not configured")
    width = min(max(view.budget_percent, 0.0), 100.0)
    return f"""<article class="metric">
      <p>Monthly budget</p><strong>{_money(view.budget_usd)}</strong>
      <span>{view.budget_percent:.1f}% used</span>
      <div class="progress" role="progressbar" aria-valuenow="{view.budget_percent:.1f}" aria-valuemin="0" aria-valuemax="100"><i style="width:{width:.1f}%"></i></div>
    </article>"""


def _baseline(view: DashboardView) -> str:
    baseline = view.baseline
    if baseline is None:
        return '<p class="empty">Not configured. Set <code>dashboard.baseline_model</code> to enable this estimate.</p>'
    direction = "Estimated savings" if baseline.savings >= 0 else "Estimated premium"
    return f"""<div class="baseline">
      <div><span>Baseline model</span><strong>{escape(baseline.model)}</strong></div>
      <div><span>Estimated baseline</span><strong>{_money(baseline.estimated_cost)}</strong></div>
      <div><span>Stored actual spend</span><strong>{_money(baseline.actual_cost)}</strong></div>
      <div><span>{direction}</span><strong>{_money(abs(baseline.savings))}</strong></div>
    </div><p class="muted">Estimate applies the baseline model's current configured rates to this month's recorded input and output tokens.</p>"""


def _breakdown(title: str, rows: list[BreakdownRow]) -> str:
    body = "".join(
        f"<tr><td>{escape(row.key)}</td><td>{row.count:,}</td><td>{_money(row.cost)}</td></tr>"
        for row in rows
    )
    if not body:
        body = '<tr><td colspan="3" class="empty">No data this month.</td></tr>'
    return f"""<article class="panel"><h3>{escape(title)}</h3><div class="table-wrap"><table>
      <thead><tr><th>Name</th><th>Requests</th><th>Spend</th></tr></thead><tbody>{body}</tbody>
    </table></div></article>"""


def _routing(title: str, rows: list[RoutingShare]) -> str:
    body = "".join(
        f"<tr><td>{escape(row.key)}</td><td>{row.count:,}</td><td>{row.share:.1f}%</td><td>{_money(row.cost)}</td></tr>"
        for row in rows
    )
    if not body:
        body = '<tr><td colspan="4" class="empty">No data this month.</td></tr>'
    return f"""<article class="panel"><h3>{escape(title)}</h3><div class="table-wrap"><table>
      <thead><tr><th>Name</th><th>Requests</th><th>Share</th><th>Spend</th></tr></thead><tbody>{body}</tbody>
    </table></div></article>"""


def _recent(rows: list[RequestListRow]) -> str:
    body = "".join(
        "<tr>"
        f"<td>{_timestamp(row.ts)}</td><td>{escape(row.pool)}</td>"
        f"<td>{escape(row.provider)} / {escape(row.model)}</td>"
        f"<td>{row.input_tokens + row.output_tokens:,}</td><td>{_money(row.cost_usd)}</td>"
        f"<td>{row.latency_ms:,} ms</td><td>{row.status}</td>"
        f"<td>{escape(row.route_stage)}</td><td>{_optional(row.route_reason)}</td>"
        "</tr>"
        for row in rows
    )
    if not body:
        body = '<tr><td colspan="9" class="empty">No requests recorded.</td></tr>'
    return f"""<div class="table-wrap"><table>
      <thead><tr><th>Time (UTC)</th><th>Pool</th><th>Provider / model</th><th>Tokens</th><th>Cost</th><th>Latency</th><th>Status</th><th>Stage</th><th>Reason</th></tr></thead>
      <tbody>{body}</tbody></table></div>"""


def _fallbacks(rows: list[RequestListRow]) -> str:
    body = "".join(
        "<tr>"
        f"<td>{_timestamp(row.ts)}</td><td>{escape(row.pool)}</td>"
        f"<td>{_optional(row.fallback_from)}</td>"
        f"<td>{escape(row.provider)} / {escape(row.model)}</td>"
        f"<td>{row.status}</td><td>{_optional(row.route_reason)}</td>"
        "</tr>"
        for row in rows
    )
    if not body:
        body = '<tr><td colspan="6" class="empty">No fallback events recorded.</td></tr>'
    return f"""<div class="table-wrap"><table>
      <thead><tr><th>Time (UTC)</th><th>Pool</th><th>Fallback from</th><th>Final provider / model</th><th>Status</th><th>Reason</th></tr></thead>
      <tbody>{body}</tbody></table></div>"""


def _optional(value: str | None) -> str:
    return escape(value) if value is not None else '<span class="muted">—</span>'


def _money(value: float) -> str:
    return f"${value:,.6f}"


def _timestamp(value: int) -> str:
    return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%d %H:%M:%S")


_CSS = """
:root{color-scheme:dark;--bg:#0b1020;--panel:#141b2d;--line:#29334a;--text:#f4f7fb;--muted:#9eabc2;--accent:#70e1b0}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top,#17213a 0,var(--bg) 42%);color:var(--text);font:14px/1.5 ui-sans-serif,system-ui,-apple-system,sans-serif}main{max-width:1440px;margin:auto;padding:40px 24px 72px}header,.section-heading{display:flex;align-items:flex-end;justify-content:space-between;gap:20px}h1{font-size:clamp(28px,4vw,46px);margin:0}h2{margin:36px 0 14px;font-size:20px}h3{margin:0 0 10px}.eyebrow{color:var(--accent);font-weight:700;letter-spacing:.12em;text-transform:uppercase;margin:0 0 5px}.muted,.metric p,.metric span,.baseline span{color:var(--muted)}.cards,.grid-2,.grid-3,.baseline{display:grid;gap:14px}.cards{grid-template-columns:repeat(4,minmax(0,1fr));margin-top:28px}.grid-2{grid-template-columns:repeat(2,minmax(0,1fr))}.grid-3{grid-template-columns:repeat(3,minmax(0,1fr))}.metric,.panel{background:rgba(20,27,45,.94);border:1px solid var(--line);border-radius:14px;padding:18px}.metric p{margin:0 0 8px}.metric strong{display:block;font-size:24px}.metric span{display:block;margin-top:6px}.progress{height:7px;background:#29334a;border-radius:8px;margin-top:12px;overflow:hidden}.progress i{display:block;height:100%;background:var(--accent)}.baseline{grid-template-columns:repeat(4,minmax(0,1fr))}.baseline div{border-left:2px solid var(--line);padding-left:12px}.baseline span,.baseline strong{display:block}.baseline strong{font-size:18px;margin-top:4px}.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:520px}th,td{text-align:left;padding:10px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em}tbody tr:last-child td{border-bottom:0}.empty{color:var(--muted);text-align:center;padding:22px}code{color:var(--accent)}@media(max-width:950px){.cards,.grid-3,.baseline{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:640px){main{padding:24px 14px 48px}header,.section-heading{display:block}.cards,.grid-2,.grid-3,.baseline{grid-template-columns:1fr}}
"""
