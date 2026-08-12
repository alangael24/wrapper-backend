-- Server-owned mapping between an Agent Genia bot and its isolated computer.
-- Provider references are never exposed through Supabase's browser-facing API.
create table if not exists agentgenia.bot_computers (
  user_id text not null references agentgenia.users(id) on delete cascade,
  bot_id text not null,
  provider text not null,
  provider_ref text,
  state text not null default 'pulling'
    check (state in ('pulling', 'running', 'hibernated', 'off', 'error')),
  last_error text,
  created_at double precision not null,
  updated_at double precision not null,
  last_active_at double precision,
  primary key (user_id, bot_id)
);

create unique index if not exists uniq_bot_computer_provider_ref
  on agentgenia.bot_computers(provider, provider_ref)
  where provider_ref is not null;
create index if not exists idx_bot_computers_user
  on agentgenia.bot_computers(user_id, updated_at desc);

alter table agentgenia.bot_computers enable row level security;
revoke all on agentgenia.bot_computers from public, anon, authenticated;

insert into agentgenia.kv(k, v) values ('schema_version', '6')
on conflict (k) do update set v = excluded.v;
