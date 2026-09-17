CREATE TABLE IF NOT EXISTS page_views (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            TEXT    NOT NULL,
  date_local    TEXT    NOT NULL,
  path          TEXT    NOT NULL,
  section       TEXT,
  city          TEXT,
  region        TEXT,
  country       TEXT,
  referrer_host TEXT,
  device        TEXT,
  is_bot        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_pv_date    ON page_views(date_local);
CREATE INDEX IF NOT EXISTS idx_pv_section ON page_views(section);
CREATE INDEX IF NOT EXISTS idx_pv_city    ON page_views(city);
