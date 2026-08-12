-- Encrypted, server-owned credentials for first-party connector adapters.
-- This schema is private and unavailable to browser-facing Supabase roles.
create table if not exists agentgenia.connector_credentials (
  user_id text not null references agentgenia.users(id) on delete cascade,
  connector_id text not null,
  credentials_enc bytea not null,
  key_id text not null,
  account_label text,
  created_at double precision not null,
  updated_at double precision not null,
  primary key (user_id, connector_id)
);

create index if not exists idx_connector_credentials_user
  on agentgenia.connector_credentials(user_id, updated_at desc);

alter table agentgenia.connector_credentials enable row level security;
revoke all on agentgenia.connector_credentials from public, anon, authenticated;

insert into agentgenia.kv(k, v) values ('schema_version', '5')
on conflict (k) do update set v = excluded.v;
