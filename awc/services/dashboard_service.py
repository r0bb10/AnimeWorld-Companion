"""Runtime dashboard composition for the clean rebuild."""

from html import escape

from ..core.config import settings
from ..repositories.sync_meta import get_sync_meta
from .catalog_service import build_catalog_snapshot
from .download_service import build_download_snapshot


def build_dashboard_snapshot() -> dict:
    return {
        "catalog": build_catalog_snapshot(show_limit=5, movie_limit=5),
        "downloads": build_download_snapshot(limit=10),
        "runtime": {
            "sonarr_configured": bool(settings.sonarr_url and settings.sonarr_api_key),
            "radarr_configured": bool(settings.radarr_url and settings.radarr_api_key),
            "animeworld_url": settings.aw_base_url,
            "last_sonarr_sync": get_sync_meta("last_sonarr_sync"),
            "last_radarr_sync": get_sync_meta("last_radarr_sync"),
        },
    }


def build_dashboard_html() -> str:
    snapshot = build_dashboard_snapshot()
    totals = snapshot["catalog"]["counts"]
    runtime = snapshot["runtime"]
    downloads = snapshot["downloads"]["downloads"]

    cards = [
        ("Shows", totals["shows"]),
        ("Movies", totals["movies"]),
        ("Show mappings", totals["show_mappings"]),
        ("Movie mappings", totals["movie_mappings"]),
        ("Active downloads", snapshot["downloads"]["counts"]["active"]),
    ]
    card_html = "".join(
        f"<div class='card'><span>{escape(label)}</span><strong>{value}</strong></div>"
        for label, value in cards
    )

    manager_html = "".join(
        (
            "<div class='panel'>"
            f"<h3>{escape(name.title())}</h3>"
            f"<p>Configured: <strong>{data['configured']}</strong></p>"
            f"<p>Last sync: <code>{escape(str(data['last_sync'] or '-'))}</code></p>"
            "</div>"
        )
        for name, data in {
            "sonarr": {
                "configured": runtime["sonarr_configured"],
                "last_sync": runtime["last_sonarr_sync"],
            },
            "radarr": {
                "configured": runtime["radarr_configured"],
                "last_sync": runtime["last_radarr_sync"],
            },
        }.items()
    )

    recent_downloads = "".join(
        (
            "<tr>"
            f"<td>{escape(item['filename'])}</td>"
            f"<td>{escape(item['status'])}</td>"
            f"<td><code>{escape(str(item['id']))}</code></td>"
            "</tr>"
        )
        for item in downloads
    ) or "<tr><td colspan='3'>No tracked downloads</td></tr>"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AWC Rebuild</title>
  <style>
    :root {{
      --bg: #f3efe7;
      --panel: #fffaf2;
      --ink: #1b1b1b;
      --muted: #665f55;
      --line: #d8cdbf;
      --accent: #a64521;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      background:
        radial-gradient(circle at top left, #fff7e8 0, transparent 28%),
        linear-gradient(180deg, #efe6d9, var(--bg));
      color: var(--ink);
    }}
    main {{
      max-width: 1100px;
      margin: 0 auto;
      padding: 32px 20px 48px;
    }}
    h1, h2, h3 {{ margin: 0 0 12px; }}
    p {{ color: var(--muted); line-height: 1.5; }}
    .hero {{
      display: grid;
      gap: 16px;
      padding: 24px;
      border: 1px solid var(--line);
      background: rgba(255, 250, 242, 0.92);
      border-radius: 20px;
      box-shadow: 0 16px 40px rgba(86, 52, 28, 0.08);
    }}
    .cards, .managers {{
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      margin-top: 20px;
    }}
    .card, .panel {{
      padding: 16px;
      border-radius: 16px;
      background: var(--panel);
      border: 1px solid var(--line);
    }}
    .card span {{
      display: block;
      color: var(--muted);
      font-size: 0.95rem;
      margin-bottom: 8px;
    }}
    .card strong {{
      font-size: 1.7rem;
      color: var(--accent);
    }}
    .grid {{
      display: grid;
      grid-template-columns: 1.3fr 1fr;
      gap: 18px;
      margin-top: 20px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
    }}
    th, td {{
      text-align: left;
      padding: 10px 8px;
      border-bottom: 1px solid var(--line);
      font-size: 0.95rem;
    }}
    code {{
      font-size: 0.85rem;
      color: var(--accent);
    }}
    .meta {{
      display: grid;
      gap: 8px;
      margin-top: 12px;
    }}
    @media (max-width: 800px) {{
      .grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <section class="hero">
      <div>
        <h1>AnimeWorld Companion Rebuild</h1>
        <p>Clean runtime focused on Sonarr and Radarr parity, live diagnostics, and the rebuilt Torznab plus download handoff path.</p>
      </div>
      <div class="meta">
        <div>AnimeWorld URL: <code>{escape(runtime['animeworld_url'] or '-')}</code></div>
        <div>Sonarr last sync: <code>{escape(str(runtime['last_sonarr_sync'] or '-'))}</code></div>
        <div>Radarr last sync: <code>{escape(str(runtime['last_radarr_sync'] or '-'))}</code></div>
      </div>
    </section>
    <section class="cards">{card_html}</section>
    <section class="managers">{manager_html}</section>
    <section class="grid">
      <div class="panel">
        <h2>Recent Downloads</h2>
        <table>
          <thead><tr><th>Filename</th><th>Status</th><th>ID</th></tr></thead>
          <tbody>{recent_downloads}</tbody>
        </table>
      </div>
      <div class="panel">
        <h2>Endpoints</h2>
        <p>The rebuild currently exposes the main operational contracts needed for container bring-up and integration testing.</p>
        <ul>
          <li><code>/api</code> Torznab entrypoint</li>
          <li><code>/download</code> fake torrent handoff</li>
          <li><code>/api/webhook</code> Sonarr and Radarr webhook intake</li>
          <li><code>/api/downloads</code> tracked download state</li>
          <li><code>/api/heartbeat</code> fast local runtime summary</li>
          <li><code>/api/rebuild/health</code> deeper remote health checks</li>
          <li><code>/api/rebuild/managers</code> manager diagnostics</li>
          <li><code>/api/rebuild/sync-overview</code> sync visibility</li>
        </ul>
      </div>
    </section>
  </main>
</body>
</html>"""
