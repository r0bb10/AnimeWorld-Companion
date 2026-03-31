"""Dashboard rendering for the rebuilt app."""

from html import escape
import json

from ..core.config import settings
from ..repositories.sync_meta import get_sync_meta
from .catalog_service import build_catalog_snapshot, build_movie_snapshot, build_show_snapshot
from .download_service import build_download_snapshot


def build_dashboard_snapshot() -> dict:
    return {
        "catalog": build_catalog_snapshot(show_limit=250, movie_limit=250),
        "downloads": build_download_snapshot(limit=30),
        "runtime": {
            "sonarr_configured": bool(settings.sonarr_url and settings.sonarr_api_key),
            "radarr_configured": bool(settings.radarr_url and settings.radarr_api_key),
            "animeworld_url": settings.aw_base_url,
            "last_sonarr_sync": get_sync_meta("last_sonarr_sync"),
            "last_radarr_sync": get_sync_meta("last_radarr_sync"),
        },
    }


def _season_has_mapping(season: dict) -> bool:
    return bool(season.get("mappings"))


def _show_counts(show: dict) -> tuple[int, int]:
    mapped = 0
    total = 0
    for season in show.get("seasons", []):
        sn = int(season.get("season_number", 0))
        if sn <= 0:
            continue
        total += 1
        if _season_has_mapping(season):
            mapped += 1
    return mapped, total


def _show_status_badge(show: dict) -> tuple[str, str]:
    mapped, total = _show_counts(show)
    if total == 0:
        return "badge-unmapped", "empty"
    if mapped == 0:
        return "badge-unmapped", "unmapped"
    if mapped == total:
        return "badge-mapped", "all mapped"
    return "badge-partial", f"{mapped}/{total}"


def _season_row(show: dict, season: dict) -> str:
    sn = int(season.get("season_number", 0))
    mappings = season.get("mappings", [])
    mapping_html = ""
    if mappings:
        parts = []
        for mapping in mappings:
            part_label = ""
            if len(mappings) > 1:
                part_label = f"<span class='mapping-part'>P{int(mapping.get('part', 1))}</span> "
            score = mapping.get("confidence_score")
            score_label = ""
            if isinstance(score, (int, float)):
                score_label = f" <span class='mapping-score'>{int(round(score * 100))}%</span>"
            parts.append(
                "<div class='mapping-line'>"
                f"{part_label}<a href='https://www.animeworld.ac/play/{escape(mapping.get('aw_link', ''))}/' target='_blank'>{escape(mapping.get('aw_link', ''))}</a>"
                f"{score_label}"
                "</div>"
            )
        mapping_html = "".join(parts)
    else:
        mapping_html = (
            "<div class='mapping-empty'>"
            f"<a href='/api/discover/{show['id']}?apikey={escape(settings.awc_api_key)}' target='_blank'>Discover candidates</a>"
            "</div>"
        )

    action_links = (
        f"<a href='/api/rebuild/shows/{show['id']}' target='_blank'>JSON</a>"
        f"<a href='/api/discover/{show['id']}?apikey={escape(settings.awc_api_key)}' target='_blank'>Discover</a>"
        f"<a href='/api/rebuild/shows/{show['id']}/mappings' target='_blank'>Mappings</a>"
    )

    return (
        "<div class='season-row'>"
        f"<div class='season-label'>S{sn:02d}</div>"
        f"<div class='season-status'>{int(season.get('episode_count', 0))} eps</div>"
        f"<div class='season-link'>{mapping_html}</div>"
        f"<div class='season-actions'>{action_links}</div>"
        "</div>"
    )


def _show_card(show_summary: dict) -> str:
    detail = build_show_snapshot(show_summary["id"])
    if not detail:
        return ""
    badge_class, badge_text = _show_status_badge(detail)
    alt_titles = [item.get("title") for item in detail.get("alternate_titles", []) if item.get("title")]
    alt_html = ""
    if alt_titles:
        preview = " / ".join(alt_titles[:3])
        extra = f" +{len(alt_titles) - 3} more" if len(alt_titles) > 3 else ""
        alt_html = f"<div class='alt-titles'>{escape(preview + extra)}</div>"

    seasons_html = "".join(_season_row(detail, season) for season in detail.get("seasons", []) if int(season.get("season_number", 0)) > 0)
    mapped, total = _show_counts(detail)
    meta_bits = [
        f"<span class='badge badge-tv'>TV</span>",
        f"<span>{int(show_summary.get('season_count', 0))} seasons</span>",
        f"<span class='badge {badge_class}'>{escape(badge_text)}</span>",
    ]
    if total and mapped < total:
        meta_bits.append(f"<span class='show-gap'>{total - mapped} missing</span>")

    return (
        f"<article class='card show-card' data-title='{escape(detail['title'].lower())}' data-status='{escape(badge_text)}'>"
        "<div class='card-header' onclick='toggleCard(this)'>"
        "<div>"
        f"<div class='card-title'>{escape(detail['title'])}</div>"
        f"{alt_html}"
        "</div>"
        f"<div class='card-meta'>{''.join(meta_bits)}<span class='arrow'>▶</span></div>"
        "</div>"
        f"<div class='card-body'>{seasons_html}</div>"
        "</article>"
    )


def _movie_card(movie_summary: dict) -> str:
    detail = build_movie_snapshot(movie_summary["id"])
    if not detail:
        return ""
    mapping = detail.get("mapping")
    badge_class = "badge-mapped" if mapping else "badge-unmapped"
    badge_text = "mapped" if mapping else "unmapped"
    mapping_html = (
        f"<a href='https://www.animeworld.ac/play/{escape(mapping.get('aw_link', ''))}/' target='_blank'>{escape(mapping.get('aw_link', ''))}</a>"
        if mapping
        else "No mapping"
    )
    return (
        f"<article class='card movie-card' data-title='{escape(detail['title'].lower())}'>"
        "<div class='card-header' onclick='toggleCard(this)'>"
        f"<div><div class='card-title'>{escape(detail['title'])}</div><div class='alt-titles'>{escape(str(detail.get('year') or '-'))} • {escape(detail.get('status') or '-')}</div></div>"
        f"<div class='card-meta'><span class='badge badge-movie'>Movie</span><span class='badge {badge_class}'>{badge_text}</span><span class='arrow'>▶</span></div>"
        "</div>"
        "<div class='card-body'>"
        "<div class='season-row'>"
        "<div class='season-label'>AW</div>"
        "<div class='season-status'>link</div>"
        f"<div class='season-link'>{mapping_html}</div>"
        f"<div class='season-actions'><a href='/api/rebuild/movies/{detail['id']}' target='_blank'>JSON</a><a href='/api/discover/movie/{detail['id']}?apikey={escape(settings.awc_api_key)}' target='_blank'>Discover</a></div>"
        "</div>"
        "</div>"
        "</article>"
    )


def _downloads_table(snapshot: dict) -> str:
    rows = []
    for item in snapshot["downloads"]["downloads"]:
        total = int(item.get("total_bytes", 0) or 0)
        downloaded = int(item.get("downloaded_bytes", 0) or 0)
        pct = int((downloaded / total) * 100) if total > 0 else 0
        size_label = f"{downloaded // (1024 * 1024)} / {total // (1024 * 1024)} MB" if total else f"{downloaded // (1024 * 1024)} MB"
        rows.append(
            "<tr>"
            f"<td class='dl-name'>{escape(item['filename'])}</td>"
            f"<td><div class='dl-bar-wrap'><div class='dl-bar' style='width:{pct}%'></div></div></td>"
            f"<td class='dl-pct'>{pct}%</td>"
            f"<td>{escape(size_label)}</td>"
            f"<td class='dl-status'>{escape(item['status'])}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='5' class='dl-empty'>No tracked downloads</td></tr>")
    return "".join(rows)


def _heartbeat(snapshot: dict) -> str:
    runtime = snapshot["runtime"]
    items = [
        ("Companion", "hb-ok", "live"),
        ("Sonarr", "hb-ok" if runtime["sonarr_configured"] else "hb-err", "configured" if runtime["sonarr_configured"] else "missing"),
        ("Radarr", "hb-ok" if runtime["radarr_configured"] else "hb-err", "configured" if runtime["radarr_configured"] else "missing"),
        ("AnimeWorld", "hb-ok" if runtime["animeworld_url"] else "hb-err", runtime["animeworld_url"] or "missing"),
    ]
    return "".join(
        f"<div class='hb-item'><span>{escape(name)}:</span><span class='hb-dot {dot}'></span><span>{escape(label)}</span></div>"
        for name, dot, label in items
    )


def build_dashboard_html() -> str:
    snapshot = build_dashboard_snapshot()
    catalog = snapshot["catalog"]
    shows = "".join(_show_card(show) for show in catalog["shows"])
    movies = "".join(_movie_card(movie) for movie in catalog["movies"])
    mapped_total = catalog["counts"]["show_mappings"] + catalog["counts"]["movie_mappings"]
    total_entities = catalog["counts"]["shows"] + catalog["counts"]["movies"]

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AnimeWorld Companion</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0d1117;color:#c9d1d9;margin:0;height:100vh;overflow:hidden}}
a{{color:#58a6ff;text-decoration:none}}a:hover{{text-decoration:underline}}
.app{{display:flex;flex-direction:column;height:100%;padding:16px}}
.topbar{{position:sticky;top:0;z-index:60;background:#0d1117;box-shadow:0 2px 6px rgba(0,0,0,.35);padding-top:8px;padding-bottom:8px}}
.content{{flex:1 1 auto;overflow-y:auto;-webkit-overflow-scrolling:touch;padding:12px 0;scrollbar-width:thin;scrollbar-color:#21262d #0d1117}}
.content::-webkit-scrollbar{{width:10px}} .content::-webkit-scrollbar-track{{background:transparent}} .content::-webkit-scrollbar-thumb{{background:#21262d;border-radius:6px}}
.header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid #21262d}}
.header h1{{font-size:20px;color:#f0f6fc}}
.header-actions{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.btn{{padding:4px 10px;border:1px solid #30363d;border-radius:6px;background:#21262d;color:#c9d1d9;font-size:12px;cursor:pointer}}
.btn:hover{{background:#30363d;border-color:#8b949e}}
.btn-sync{{background:#1f6feb;border-color:#1f6feb;color:#fff}}
.btn-primary{{background:#238636;border-color:#238636;color:#fff}}
.btn-danger{{background:#da3633;border-color:#da3633;color:#fff}}
.heartbeat{{display:flex;gap:12px;flex-wrap:wrap;font-size:12px;margin-bottom:12px;padding:8px 12px;background:#161b22;border:1px solid #21262d;border-radius:6px}}
.hb-item{{display:flex;align-items:center;gap:6px}}
.hb-dot{{width:8px;height:8px;border-radius:50%;display:inline-block}}
.hb-ok{{background:#3fb950}} .hb-err{{background:#da3633}}
.dl-box{{margin-bottom:12px;background:#161b22;border:1px solid #21262d;border-radius:8px;overflow:hidden}}
.dl-header{{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;border-bottom:1px solid #21262d}}
.dl-header h3{{font-size:13px;color:#8b949e;font-weight:600;margin:0}}
.dl-table{{width:100%;border-collapse:collapse;font-size:12px}}
.dl-table th{{text-align:left;padding:6px 10px;color:#8b949e;font-weight:600;border-bottom:1px solid #21262d;font-size:11px}}
.dl-table td{{padding:6px 10px;border-bottom:1px solid #161b22}}
.dl-name{{max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#f0f6fc;font-weight:500}}
.dl-bar-wrap{{height:6px;background:#21262d;border-radius:3px;overflow:hidden;display:block;width:100%}}
.dl-bar{{height:100%;background:#238636;border-radius:3px}}
.dl-pct{{font-variant-numeric:tabular-nums;text-align:center;color:#58a6ff;font-weight:600}}
.dl-empty{{color:#8b949e;text-align:center}}
.stats{{font-size:13px;color:#8b949e;margin-bottom:16px}}
.stats span{{color:#58a6ff;font-weight:600}}
.filter-bar{{display:flex;gap:8px;margin-bottom:16px;align-items:center}}
.filter-bar input{{flex:1;padding:6px 10px;background:#161b22;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;font-size:14px}}
.filter-links{{display:flex;gap:6px;flex-wrap:wrap}}
.card{{background:#161b22;border:1px solid #21262d;border-radius:8px;margin-bottom:8px;overflow:hidden}}
.card-header{{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;cursor:pointer;user-select:none}}
.card-header:hover{{background:#1c2128}}
.card-title{{font-size:15px;font-weight:600;color:#f0f6fc}}
.card-meta{{display:flex;gap:8px;align-items:center;font-size:12px;color:#8b949e;flex-wrap:wrap;justify-content:flex-end}}
.alt-titles{{font-size:11px;color:#8b949e;margin-top:2px}}
.badge{{display:inline-block;padding:2px 6px;border-radius:10px;font-size:11px;font-weight:600;line-height:1.2;vertical-align:middle}}
.badge-mapped{{background:#238636;color:#fff}} .badge-partial{{background:#9e6a03;color:#fff}} .badge-unmapped{{background:#da3633;color:#fff}}
.badge-tv{{background:#1f6feb;color:#fff}} .badge-movie{{background:#6e40c9;color:#fff}}
.show-gap{{color:#d29922}}
.card-body{{display:none;padding:0 14px 12px;border-top:1px solid #21262d}}
.card.open .card-body{{display:block}}
.season-row{{display:flex;align-items:flex-start;gap:10px;padding:8px 0;border-bottom:1px solid #21262d;font-size:13px}}
.season-row:last-child{{border-bottom:none}}
.season-label{{width:50px;font-weight:600;color:#f0f6fc;flex-shrink:0}}
.season-status{{width:70px;color:#8b949e;flex-shrink:0}}
.season-link{{flex:1;min-width:0}}
.season-actions{{display:flex;gap:8px;flex-shrink:0;flex-wrap:wrap}}
.mapping-line{{margin-bottom:4px;overflow-wrap:anywhere}}
.mapping-part{{font-weight:600;margin-right:6px;color:#f0f6fc}}
.mapping-score{{font-size:11px;color:#d29922;margin-left:6px}}
.mapping-empty{{color:#8b949e}}
.movies-wrap{{margin-top:18px;padding-top:12px;border-top:1px solid #21262d}}
.section-heading{{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;color:#8b949e;font-size:13px}}
.arrow{{transition:transform .2s;display:inline-block}}
.card.open .arrow{{transform:rotate(90deg)}}
.hidden{{display:none!important}}
@media(max-width:768px){{
  .app{{padding:10px}}
  .header{{flex-direction:column;align-items:flex-start;gap:8px}}
  .heartbeat{{display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:11px}}
  .card-header{{flex-wrap:wrap;gap:4px}}
  .season-row{{flex-wrap:wrap;row-gap:6px}}
  .season-link{{order:3;flex:0 0 100%}}
  .season-actions{{order:4;flex:0 0 100%}}
  .filter-bar{{flex-direction:column;align-items:stretch}}
}}
</style>
</head>
<body>
<div class="app">
  <div class="topbar">
    <div class="header">
      <h1>AnimeWorld Companion</h1>
      <div class="header-actions">
        <button class="btn btn-sync" onclick="postAction('/sync')">Sync</button>
        <button class="btn btn-primary" onclick="postAction('/automap')">Automap Library</button>
        <button class="btn" onclick="postAction('/api/links/sanitize')">Sanitize Links</button>
        <button class="btn btn-danger" onclick="postAction('/restart')">Restart</button>
      </div>
    </div>

    <div class="heartbeat">{_heartbeat(snapshot)}</div>

    <div class="dl-box">
      <div class="dl-header">
        <h3>Downloads</h3>
        <div class="filter-links">
          <a class="btn" href="/api/downloads?apikey={escape(settings.awc_api_key)}" target="_blank">JSON</a>
          <a class="btn" href="/api/rss/cache?apikey={escape(settings.awc_api_key)}" target="_blank">RSS Cache</a>
        </div>
      </div>
      <table class="dl-table">
        <thead><tr><th style="width:34%">File</th><th style="width:28%">Progress</th><th style="width:8%">%</th><th style="width:12%">Size</th><th style="width:18%">Status</th></tr></thead>
        <tbody>{_downloads_table(snapshot)}</tbody>
      </table>
    </div>

    <div class="stats">
      <span>{catalog['counts']['shows']}</span> shows
      &middot; <span>{catalog['counts']['movies']}</span> movies
      &middot; mapped <span>{mapped_total}</span>/<span>{total_entities}</span>
      &middot; Sonarr sync <span>{escape(str(snapshot['runtime']['last_sonarr_sync'] or '-'))}</span>
    </div>

    <div class="filter-bar">
      <input type="text" id="search" placeholder="Filter shows and movies..." oninput="filterCards()">
      <div class="filter-links">
        <a class="btn" href="/api/rebuild/catalog" target="_blank">Catalog JSON</a>
        <a class="btn" href="/api/rebuild/status?apikey={escape(settings.awc_api_key)}" target="_blank">Status</a>
      </div>
    </div>
  </div>

  <main class="content">
    <section id="shows-list">{shows}</section>
    <section class="movies-wrap">
      <div class="section-heading"><span>Movies</span><span>{catalog['counts']['movies']} total</span></div>
      <section id="movies-list">{movies}</section>
    </section>
  </main>
</div>

<script>
const API_KEY = {json.dumps(settings.awc_api_key)};
function toggleCard(el) {{
  el.parentElement.classList.toggle('open');
}}
function filterCards() {{
  const q = document.getElementById('search').value.trim().toLowerCase();
  const cards = document.querySelectorAll('.show-card, .movie-card');
  for (const card of cards) {{
    const text = card.getAttribute('data-title') || '';
    card.classList.toggle('hidden', q && !text.includes(q));
  }}
}}
async function postAction(path) {{
  const url = `${{path}}?apikey=${{encodeURIComponent(API_KEY)}}`;
  try {{
    const response = await fetch(url, {{ method: 'POST' }});
    const text = await response.text();
    if (!response.ok) {{
      alert(`Request failed: ${{response.status}}\\n${{text}}`);
      return;
    }}
    alert(text);
  }} catch (error) {{
    alert(`Request failed: ${{error}}`);
  }}
}}
</script>
</body>
</html>"""
