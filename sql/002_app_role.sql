-- ============================================================
-- 配布アプリ用のユーザー(ロール)を作る  002_app_role
--
-- なぜ必要か:
--   接続情報は zip に同梱してメンバー全員に配ることになる。
--   管理者ユーザー(postgres)をそのまま配ると、テーブル削除など
--   何でもできてしまう。読み書きだけに絞った専用ユーザーを配る。
--
-- 実行前にやること:
--   下の 'ここにパスワードを入れる' を、自分で決めたパスワードに置き換える。
--   （Supabaseのログインパスワードとも、DBの管理者パスワードとも別のものにする）
--
-- 実行方法:
--   Supabase の SQL Editor に貼って実行する。
-- ============================================================

-- ① ログインできるユーザーを作る
create role reins_app with login password 'ここにパスワードを入れる';

-- ② reins スキーマを見られるようにする
grant usage on schema reins to reins_app;

-- ③ 既存テーブルへの権限（削除(delete)とテーブル操作(drop)は与えない）
grant select, insert, update on all tables in schema reins to reins_app;

-- ④ bigserial（自動採番）を使うために必要
grant usage, select on all sequences in schema reins to reins_app;

-- ⑤ 今後テーブルを追加したときも、同じ権限が自動で付くようにする
alter default privileges in schema reins
  grant select, insert, update on tables to reins_app;
alter default privileges in schema reins
  grant usage, select on sequences to reins_app;

-- 確認用（実行すると権限一覧が出る）
-- select grantee, table_name, privilege_type
-- from information_schema.role_table_grants
-- where table_schema = 'reins' and grantee = 'reins_app'
-- order by table_name, privilege_type;
