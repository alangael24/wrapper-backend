-- Exact, durable, one-shot approval capabilities for external side effects.
create table if not exists agentgenia.pending_approvals (
  id             text primary key,
  action_id      text unique not null,
  user_id        text not null references agentgenia.users(id) on delete cascade,
  bot_id         text not null,
  run_id         text not null references agentgenia.agent_runs(id) on delete cascade,
  target_type    text not null check (target_type in ('connector', 'computer')),
  connector_id   text not null,
  operation      text not null,
  arguments_json text not null,
  arguments_hash text not null,
  human_summary  text not null,
  status         text not null default 'pending'
    check (status in ('pending','approved','rejected','dispatched','succeeded','uncertain','expired')),
  expires_at     double precision not null,
  approved_at    double precision,
  consumed_at    double precision,
  created_at     double precision not null,
  updated_at     double precision not null,
  unique (user_id, run_id, target_type, connector_id, operation, arguments_hash)
);

create index if not exists idx_pending_approvals_run
  on agentgenia.pending_approvals(user_id, run_id, status, created_at);
create index if not exists idx_pending_approvals_bot
  on agentgenia.pending_approvals(user_id, bot_id, status, expires_at);

alter table agentgenia.pending_approvals enable row level security;
revoke all on agentgenia.pending_approvals
  from public, anon, authenticated, service_role;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'agentgenia_app') then
    grant select, insert, update, delete
      on agentgenia.pending_approvals to agentgenia_app;
  end if;
end
$$;

insert into agentgenia.kv(k, v) values ('schema_version', '20')
on conflict (k) do update set v = excluded.v;
