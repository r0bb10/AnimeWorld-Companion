"""OpenAPI metadata for production-facing API docs."""

API_DESCRIPTION = """
AnimeWorld Companion bridges Sonarr, Radarr, and AnimeWorld.

Use this API to:
- browse synced shows and movies
- discover AnimeWorld candidates
- trigger automap for one item or the whole library
- manage mappings, downloads, RSS cache, and sanitizer jobs
- expose a Torznab-compatible `/api` endpoint to Arr clients

Authentication:
- pass `apikey` as a query parameter, or
- pass `X-Api-Key` as a header

Most automation happens through:
- `/api/automap`
- `/api/sync`, `/api/sync/sonarr`, `/api/sync/radarr`
- `/api/webhook`
- `/api/downloads`
- `/api/mappings/unmap-all`
- `/api?t=caps|tvsearch|search|movie`
""".strip()


OPENAPI_TAGS = [
    {
        "name": "Indexer",
        "description": "Torznab-compatible endpoint used by Sonarr and Radarr.",
    },
    {
        "name": "Catalog",
        "description": "Read the synced AWC library of shows and movies.",
    },
    {
        "name": "Discovery",
        "description": "Search AnimeWorld and inspect candidate links before mapping.",
    },
    {
        "name": "Mutation",
        "description": "Manual mapping, ignore/unignore, and delete actions.",
    },
    {
        "name": "Automap",
        "description": "Automatic mapping workflows for one item, one season, or the full library.",
    },
    {
        "name": "Download",
        "description": "Torrent handoff, tracked downloads, and download lifecycle actions.",
    },
    {
        "name": "Integration",
        "description": "Sync and webhook endpoints for Sonarr and Radarr.",
    },
    {
        "name": "System",
        "description": "Health, runtime status, RSS cache, sanitizer, heartbeat, and restart helpers.",
    },
    {
        "name": "UI",
        "description": "Dashboard HTML endpoint.",
    },
]
