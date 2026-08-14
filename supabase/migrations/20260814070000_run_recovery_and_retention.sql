-- Durable agent results and retention indexes.
alter table agentgenia.agent_runs
  add column if not exists result_json text;

create index if not exists idx_usage_events_created
  on agentgenia.usage_events(created_at);

insert into agentgenia.kv(k, v) values ('schema_version', '16')
on conflict (k) do update set v = excluded.v;
