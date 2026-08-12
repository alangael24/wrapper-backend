-- Persistent, one-time auth attempts; account revocation; versioned envelope keys.
alter table agentgenia.users
  add column if not exists account_status text not null default 'active',
  add column if not exists disabled_at double precision;

alter table agentgenia.go_subscriptions
  add column if not exists key_version integer not null default 1;

alter table agentgenia.connector_credentials
  add column if not exists key_version integer not null default 1;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'users_account_status_valid' and conrelid = 'agentgenia.users'::regclass) then
    alter table agentgenia.users add constraint users_account_status_valid
      check (account_status in ('active', 'disabled'));
  end if;
  if not exists (select 1 from pg_constraint where conname = 'go_subscriptions_key_version_positive' and conrelid = 'agentgenia.go_subscriptions'::regclass) then
    alter table agentgenia.go_subscriptions add constraint go_subscriptions_key_version_positive
      check (key_version > 0);
  end if;
  if not exists (select 1 from pg_constraint where conname = 'connector_credentials_key_version_positive' and conrelid = 'agentgenia.connector_credentials'::regclass) then
    alter table agentgenia.connector_credentials add constraint connector_credentials_key_version_positive
      check (key_version > 0);
  end if;
end $$;

create table if not exists agentgenia.account_auth_attempts (
  id_hash text primary key,
  state_hash text unique not null,
  device_id_hash text not null,
  verifier_enc bytea not null,
  result_enc bytea,
  key_version integer not null default 1 check (key_version > 0),
  status text not null default 'pending'
    check (status in ('pending', 'exchanging', 'complete', 'error')),
  message text not null default '',
  expires_at double precision not null,
  consumed_at double precision,
  created_at double precision not null,
  updated_at double precision not null
);

create table if not exists agentgenia.connector_auth_attempts (
  id_hash text primary key,
  user_id text not null references agentgenia.users(id) on delete cascade,
  connector_id text not null,
  driver text not null check (driver in ('composio', 'native')),
  connected_account_id text,
  status text not null default 'pending'
    check (status in ('pending', 'complete', 'error')),
  account_label text not null default '',
  message text not null default '',
  expires_at double precision not null,
  next_poll_at double precision not null default 0,
  consumed_at double precision,
  created_at double precision not null,
  updated_at double precision not null
);

create index if not exists idx_account_auth_attempts_expires
  on agentgenia.account_auth_attempts(expires_at);
create index if not exists idx_connector_auth_attempts_user
  on agentgenia.connector_auth_attempts(user_id, expires_at);

alter table agentgenia.account_auth_attempts enable row level security;
alter table agentgenia.connector_auth_attempts enable row level security;
revoke all on agentgenia.account_auth_attempts from public, anon, authenticated;
revoke all on agentgenia.connector_auth_attempts from public, anon, authenticated;

insert into agentgenia.kv(k, v) values ('schema_version', '8')
on conflict (k) do update set v = excluded.v;
