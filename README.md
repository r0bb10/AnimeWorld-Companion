# AnimeWorld Companion

Ho creato questo progetto per uso strettamente personale che ho deciso di condividere, è da ritenersi beta anche se funziona discretamente bene.

AnimeWorld Companion (AWC) era inizialmente un semplice indexer per Sonarr che interfacciava il portale AnimeWorld grazie all' [AnimeWorld-API](https://github.com/MainKronos/AnimeWorld-API) di MainKronos (grande!) emulando il protocollo Torznab, con l'aggiunta del downloader (modalità torrent blackhole) è diventato un vero e proprio companion di Sonarr comunicandoci bi-direzionalmente.

> **Non mi dilungo qui per non ingigantire il README più del dovuto, ci saranno spiegazioni sulla logica e funzioni più in dettaglio nella [Wiki](https://github.com/r0bb10/AnimeWorld-Companion/wiki).**

## Setup

### 1. Docker Compose Minimale

```yaml
services:
  awc:
    image: ghcr.io/r0bb10/animeworld-companion:latest
    container_name: awc
    ports:
      - "7004:7004"
    env_file:
      - .env
    volumes:
      - ./config:/config
      - ./data:/data
    restart: unless-stopped
```

### 2. `.env`

```env

# --- AnimeWorld ---
AW_BASE_URL=https://www.animeworld.ac

# --- Server ---
AWC_URL=http://<ip-host>:7004
AWC_PORT=7004
AWC_API_KEY=cambia_questa_chiave

# --- Sonarr ---
SONARR_URL=http://<ip-sonarr>:8989
SONARR_API_KEY=la_tua_api_key_sonarr
SONARR_ANIME_TAG=anime
SONARR_DUB_TAG=ita

# --- RSS ---
RSS_ENABLED=true
SONARR_UNMONITOR_IMPORTED=true
```

### 3. Configurazione Sonarr

- **Indexer:** Impostazioni → Indexer → Aggiungi → Torznab
  - URL: `http://<ip-host>:7004/api`
  - API Key: valore di `AWC_API_KEY`
  - Anime Categories: `5070 (Anime)`
  - Tags: `anime`

- **Download client:** Impostazioni → Download client → Aggiungi → Torrent Blackhole
  - Torrent folder: `/data` (non verrà creato alcun file .torrent)
  - Watch folder: `/data` (percorso interno al container Sonarr che punta alla stessa cartella `./data`)
  - Tags: `anime`

- **Webhook:** Impostazioni → Connetti → Aggiungi → Webhook
  - Trigger: `On Series Add, On Seried Delete`
  - Tags: `anime`
  - URL: `http://<ip-host>:7004/api/webhook?apikey=cambia_questa_chiave`
  - Method: `POST`

- **Profile:** Impostazioni → Profili → Aggiungi
  - Name: `Fansub`
  - Upgrade: `[ ]` (senza spunta)
  - Quality: `HDTV-720p` e `SDTV`