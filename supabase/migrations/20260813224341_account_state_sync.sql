-- Canonical account-scoped product state for desktop and mobile clients.
-- Only the wrapper backend can read it; clients use authenticated API routes.
create table if not exists agentgenia.account_states (
  user_id text primary key references agentgenia.users(id) on delete cascade,
  revision bigint not null default 1 check (revision > 0),
  state_json text not null,
  updated_by_device_hash text not null,
  created_at double precision not null,
  updated_at double precision not null
);

create index if not exists idx_account_states_updated
  on agentgenia.account_states(updated_at desc);

alter table agentgenia.account_states enable row level security;
revoke all on agentgenia.account_states from public, anon, authenticated;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'agentgenia_app') then
    grant select, insert, update, delete
      on agentgenia.account_states to agentgenia_app;
  end if;
end
$$;

insert into agentgenia.kv(k, v) values ('schema_version', '13')
on conflict (k) do update set v = excluded.v;
