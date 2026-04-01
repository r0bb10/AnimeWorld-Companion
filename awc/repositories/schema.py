"""Database schema initialisation for AW-Companion."""

from .db import get_db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shows (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    sonarr_id         INTEGER UNIQUE,
    tvdb_id           INTEGER,
    title             TEXT NOT NULL,
    sort_title        TEXT,
    series_type       TEXT DEFAULT 'standard',
    monitored         BOOLEAN DEFAULT 1,
    status            TEXT,
    year              INTEGER,
    original_language TEXT,
    first_aired       DATE,
    genres            TEXT,
    created_at        TEXT DEFAULT (datetime('now')),
    updated_at        TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_shows_sonarr_id ON shows(sonarr_id);
CREATE INDEX IF NOT EXISTS idx_shows_tvdb_id   ON shows(tvdb_id);
CREATE INDEX IF NOT EXISTS idx_shows_title     ON shows(title COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS show_alternate_titles (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    show_id             INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    title               TEXT NOT NULL,
    title_normalized    TEXT NOT NULL,
    source              TEXT DEFAULT 'sonarr',
    title_type          TEXT,
    language            TEXT,
    scene_season_number INTEGER,
    title_year          INTEGER,
    created_at          TEXT DEFAULT (datetime('now')),
    UNIQUE(show_id, title_normalized)
);

CREATE INDEX IF NOT EXISTS idx_alt_normalized ON show_alternate_titles(title_normalized);

CREATE TABLE IF NOT EXISTS show_seasons (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    show_id        INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    season_number  INTEGER NOT NULL,
    monitored      BOOLEAN DEFAULT 1,
    episode_count  INTEGER DEFAULT 0,
    air_date_start DATE,
    air_date_end   DATE,
    ignored        BOOLEAN DEFAULT 0,
    created_at     TEXT DEFAULT (datetime('now')),
    updated_at     TEXT DEFAULT (datetime('now')),
    UNIQUE(show_id, season_number)
);

CREATE INDEX IF NOT EXISTS idx_seasons_show ON show_seasons(show_id);

CREATE TABLE IF NOT EXISTS aw_show_mappings (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    show_id            INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    season_number      INTEGER NOT NULL,
    part               INTEGER DEFAULT 1,
    aw_link            TEXT NOT NULL,
    aw_title           TEXT,
    aw_episode_count   INTEGER DEFAULT 0,
    aw_total_episodes  INTEGER DEFAULT 0,
    aw_status          TEXT,
    aw_category        TEXT,
    mapping_type       TEXT DEFAULT 'auto',
    confidence_score   REAL DEFAULT 0.0,
    confidence_factors  TEXT,
    link_check_failures INTEGER DEFAULT 0,
    linked_with_season  INTEGER,
    last_verified       TIMESTAMP,
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_aw_map_show        ON aw_show_mappings(show_id);
CREATE INDEX IF NOT EXISTS idx_aw_map_show_season ON aw_show_mappings(show_id, season_number);
CREATE INDEX IF NOT EXISTS idx_aw_map_link        ON aw_show_mappings(aw_link);

CREATE TABLE IF NOT EXISTS sync_metadata (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS show_scene_episodes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    show_id          INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    scene_season     INTEGER NOT NULL,
    scene_episode    INTEGER NOT NULL,
    internal_season  INTEGER NOT NULL,
    internal_episode INTEGER NOT NULL,
    absolute_episode INTEGER,
    created_at       TEXT DEFAULT (datetime('now')),
    updated_at       TEXT DEFAULT (datetime('now')),
    UNIQUE(show_id, scene_season, scene_episode)
);

CREATE INDEX IF NOT EXISTS idx_scene_map_show     ON show_scene_episodes(show_id);
CREATE INDEX IF NOT EXISTS idx_scene_map_lookup   ON show_scene_episodes(show_id, scene_season, scene_episode);
CREATE INDEX IF NOT EXISTS idx_scene_map_absolute ON show_scene_episodes(show_id, absolute_episode);

CREATE TABLE IF NOT EXISTS show_rss_cache (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    show_id        INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    season_number  INTEGER NOT NULL,
    episode_number INTEGER NOT NULL,
    title          TEXT NOT NULL,
    guid           TEXT NOT NULL,
    size           TEXT DEFAULT '0',
    pub_date       TEXT,
    aw_episode_link TEXT,
    created_at     TEXT DEFAULT (datetime('now')),
    UNIQUE(show_id, season_number, episode_number)
);

CREATE INDEX IF NOT EXISTS idx_rss_cache_show_season ON show_rss_cache(show_id, season_number);
CREATE INDEX IF NOT EXISTS idx_rss_cache_created     ON show_rss_cache(created_at);

CREATE TABLE IF NOT EXISTS movies (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    radarr_id         INTEGER UNIQUE NOT NULL,
    tmdb_id           INTEGER,
    imdb_id           TEXT,
    title             TEXT NOT NULL,
    sort_title        TEXT,
    monitored         INTEGER DEFAULT 1,
    status            TEXT,
    year              INTEGER,
    original_language TEXT,
    first_aired       TEXT,
    genres            TEXT,
    created_at        TEXT DEFAULT (datetime('now')),
    updated_at        TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_movies_radarr_id ON movies(radarr_id);
CREATE INDEX IF NOT EXISTS idx_movies_tmdb_id   ON movies(tmdb_id);
CREATE INDEX IF NOT EXISTS idx_movies_title     ON movies(title COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS movie_alternate_titles (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    movie_id         INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
    title            TEXT NOT NULL,
    title_normalized TEXT NOT NULL,
    source           TEXT DEFAULT 'radarr',
    language         TEXT,
    created_at       TEXT DEFAULT (datetime('now')),
    UNIQUE(movie_id, title_normalized)
);

CREATE INDEX IF NOT EXISTS idx_movie_alt_normalized ON movie_alternate_titles(title_normalized);

CREATE TABLE IF NOT EXISTS aw_movie_mappings (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    movie_id           INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
    aw_link            TEXT NOT NULL,
    aw_title           TEXT,
    aw_status          TEXT,
    aw_category        TEXT,
    mapping_type       TEXT DEFAULT 'auto',
    confidence_score   REAL DEFAULT 0.0,
    confidence_factors TEXT,
    link_check_failures INTEGER DEFAULT 0,
    last_verified      TIMESTAMP,
    created_at         TEXT DEFAULT (datetime('now')),
    updated_at         TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_aw_movie_map_movie ON aw_movie_mappings(movie_id);
CREATE INDEX IF NOT EXISTS idx_aw_movie_map_link  ON aw_movie_mappings(aw_link);

CREATE TABLE IF NOT EXISTS downloads (
    id               TEXT PRIMARY KEY,
    url              TEXT NOT NULL,
    filename         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'queued',
    total_bytes      INTEGER DEFAULT 0,
    downloaded_bytes INTEGER DEFAULT 0,
    part_path        TEXT DEFAULT '',
    error            TEXT DEFAULT '',
    started_at       REAL,
    finished_at      REAL,
    created_at       REAL NOT NULL,
    sonarr_id        INTEGER,
    radarr_id        INTEGER
);

CREATE INDEX IF NOT EXISTS idx_downloads_status  ON downloads(status);
CREATE INDEX IF NOT EXISTS idx_downloads_created ON downloads(created_at DESC);
"""


def init_db() -> None:
    """Create all tables and indexes if they do not already exist."""
    with get_db(write=True) as conn:
        conn.executescript(_SCHEMA)
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(downloads)").fetchall()
        }
        if "radarr_id" not in columns:
            conn.execute("ALTER TABLE downloads ADD COLUMN radarr_id INTEGER")
