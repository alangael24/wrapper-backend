-- One-time Apple identity tokens and encrypted provider refresh credentials.
-- Both tables remain server-owned inside the private Agent Genia schema.
create table if not exists agentgenia.account_identity_tokens (
  token_hash text primary key,
  provider text not null check (provider in ('apple')),
  expires_at double precision not null,
  created_at double precision not null
);

create table if not exists agentgenia.account_provider_credentials (
  account_id text not null references agentgenia.account_identities(id) on delete cascade,
  provider text not null check (provider in ('apple')),
  credential_enc bytea not null,
  key_id text not null,
  key_version integer not null default 1 check (key_version > 0),
  created_at double precision not null,
  updated_at double precision not null,
  primary key (account_id, provider)
);

create index if not exists idx_account_identity_tokens_expires
  on agentgenia.account_identity_tokens(expires_at);
create index if not exists idx_account_provider_credentials_account
  on agentgenia.account_provider_credentials(account_id);

alter table agentgenia.account_identity_tokens enable row level security;
alter table agentgenia.account_provider_credentials enable row level security;
revoke all on agentgenia.account_identity_tokens from public, anon, authenticated;
revoke all on agentgenia.account_provider_credentials from public, anon, authenticated;

insert into agentgenia.kv(k, v) values ('schema_version', '9')
on conflict (k) do update set v = excluded.v;
