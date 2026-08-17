-- Durable, run-scoped idempotency for connector provider calls.
create table if not exists agentgenia.connector_operations (
  user_id        text not null references agentgenia.users(id) on delete cascade,
  run_id         text not null references agentgenia.agent_runs(id) on delete cascade,
  operation_id   text not null,
  connector_id   text not null,
  operation      text not null,
  arguments_hash text not null,
  status         text not null default 'running'
    check (status in ('running', 'succeeded', 'failed')),
  result_json    text,
  error_code     text,
  created_at     double precision not null,
  updated_at     double precision not null,
  primary key (user_id, run_id, operation_id)
);

create index if not exists idx_connector_operations_run
  on agentgenia.connector_operations(run_id, created_at);

alter table agentgenia.connector_operations enable row level security;
revoke all on agentgenia.connector_operations
  from public, anon, authenticated, service_role;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'agentgenia_app') then
    grant select, insert, update, delete
      on agentgenia.connector_operations to agentgenia_app;
  end if;
end
$$;

insert into agentgenia.kv(k, v) values ('schema_version', '18')
on conflict (k) do update set v = excluded.v;
