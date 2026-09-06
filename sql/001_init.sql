-- ============================================================
-- REINS物件監視 Supabase(PostgreSQL) スキーマ  001_init
--
-- 第1段階の位置づけ:
--   スプレッドシートが「正」、こちらは「写し」。
--   写しが安定して一致することを確認してから、正を移す。
--
-- 実行方法:
--   Supabase の SQL Editor にこのファイルの内容を貼って実行する。
--   何度実行しても壊れないよう if not exists を付けてある。
-- ============================================================

create schema if not exists reins;

-- ------------------------------------------------------------
-- 駅・路線マスタ
--   1駅が複数路線に属し、1路線が複数駅を持つ（多対多）。
--   顧客の「沿線希望」で物件を引くための土台。
--   投入は後工程だが、properties から参照するので先に作る。
-- ------------------------------------------------------------

create table if not exists reins.rail_lines (
  id        bigserial primary key,
  operator  text,                       -- 事業者名（東京メトロ など）
  line_name text not null,
  unique (operator, line_name)
);

create table if not exists reins.stations (
  id           bigserial primary key,
  station_name text not null,
  lat          double precision,
  lon          double precision
);
create index if not exists idx_stations_name on reins.stations (station_name);

create table if not exists reins.line_stations (
  line_id    bigint references reins.rail_lines(id) on delete cascade,
  station_id bigint references reins.stations(id)   on delete cascade,
  seq        int,                       -- 路線内の駅順（起点から）
  primary key (line_id, station_id)
);

-- REINSの表記ゆれ（"東西線/西葛西" など）を駅に結びつけるための別名表。
-- 正規化で拾えなかった表記をここに足していく運用にする。
create table if not exists reins.station_aliases (
  alias      text primary key,
  station_id bigint references reins.stations(id) on delete cascade
);

-- ------------------------------------------------------------
-- 物件
--   「物件DB」と「成約・取消」を1テーブルに統合し status で区別する。
--   シート間の行移動が status の更新になるので、整合が崩れない。
--
--   数値列は解析できたものだけ入れ、解析できなければ null。
--   元のテキストは raw(jsonb) に全列そのまま残すので情報は失われない。
-- ------------------------------------------------------------

create table if not exists reins.properties (
  property_no       text primary key,
  status            text not null,          -- アクティブ / 取消候補 / 成約・取消
  property_type     text,
  deal_status       text,
  deal_type         text,
  address           text,
  building_name     text,
  floor_info        text,
  layout            text,

  price             numeric,                -- 万円
  exclusive_area    numeric,
  building_area     numeric,
  land_area         numeric,
  unit_price_sqm    numeric,
  unit_price_tsubo  numeric,
  management_fee    numeric,

  zoning            text,
  building_coverage text,
  floor_area_ratio  text,
  road_access       text,
  road_1            text,

  line_station      text,                   -- REINS原文（正規化前）
  transport         text,                   -- REINS原文（正規化前）
  station_id        bigint references reins.stations(id),
  walk_min          int,

  built_ym          text,
  built_year        int,
  agency_name       text,
  agency_tel        text,
  drawing_path      text,
  source_conditions text,                   -- 検出条件（文字列のまま）
  group_id          text,
  identity_key      text,                   -- 物件番号が変わっても同一物件を追うためのキー

  listed_on         date,
  first_seen_on     date,
  last_seen_on      date,
  candidate_since   date,
  miss_count        int default 0,
  closed_on         date,

  raw               jsonb,                  -- スクレイプ時の全列を原文で保持
  updated_at        timestamptz default now()
);

create index if not exists idx_prop_status   on reins.properties (status);
create index if not exists idx_prop_station  on reins.properties (station_id);
create index if not exists idx_prop_price    on reins.properties (price);
create index if not exists idx_prop_lastseen on reins.properties (last_seen_on);
create index if not exists idx_prop_group    on reins.properties (group_id);
create index if not exists idx_prop_identity on reins.properties (identity_key);

-- ------------------------------------------------------------
-- 変更ログ
--   スプシでは行数対策で年別シートに分けていたが、
--   インデックスがあるので1テーブルで問題ない。
-- ------------------------------------------------------------

create table if not exists reins.change_log (
  id             bigserial primary key,
  logged_at      timestamptz not null,
  condition_name text,
  change_type    text,                      -- 新規 / 価格変更 / 取消候補 など
  property_no    text,
  address        text,
  price          text,
  old_price      text
);
create index if not exists idx_log_property on reins.change_log (property_no);
create index if not exists idx_log_time     on reins.change_log (logged_at);

-- ------------------------------------------------------------
-- 実行履歴
--   「今週の取消候補は多いのか」をログのgrepではなくクエリで判断するため。
--   配線は後工程（第1段階では器だけ用意する）。
-- ------------------------------------------------------------

create table if not exists reins.runs (
  id                  bigserial primary key,
  started_at          timestamptz,
  finished_at         timestamptz default now(),
  mode                text,                 -- half_morning / half_weekly など
  scraped_count       int,
  new_count           int,
  price_changed_count int,
  candidate_count     int,
  closed_count        int
);
create index if not exists idx_runs_time on reins.runs (finished_at);
