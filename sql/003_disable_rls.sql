-- ============================================================
-- reins スキーマの RLS(行レベルセキュリティ) を無効にする  003
--
-- なぜ必要か:
--   プロジェクト作成時の設定で、新規テーブルに自動でRLSが付いた。
--   RLSは「許可ルール(ポリシー)を書かない限り全部拒否」なので、
--   ポリシー0件の状態では reins_app が1行も書き込めない。
--
-- なぜ無効にしてよいか:
--   RLSはブラウザから直接DBを触る構成(Data API + anonキー)向けの仕組み。
--   今回はデスクトップのプログラムが専用ユーザー reins_app で接続し、
--   権限は GRANT で既に絞ってある(削除・テーブル操作は不可)。
--   また reins スキーマは Data API に公開していないので外部からは触れない。
--
-- 実行方法:
--   Supabase の SQL Editor に貼って Run。
--   テーブルの所有者(postgres)権限が要るので、SQL Editorから実行すること。
-- ============================================================

alter table reins.properties      disable row level security;
alter table reins.change_log      disable row level security;
alter table reins.runs            disable row level security;
alter table reins.stations        disable row level security;
alter table reins.rail_lines      disable row level security;
alter table reins.line_stations   disable row level security;
alter table reins.station_aliases disable row level security;

-- 確認: rls_enabled が全部 false になっていればOK
select c.relname as table_name,
       c.relrowsecurity as rls_enabled
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'reins' and c.relkind = 'r'
order by c.relname;
