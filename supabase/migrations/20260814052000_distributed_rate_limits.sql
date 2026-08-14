-- Atomic rate-limit buckets shared by every backend replica.
create table if not exists agentgenia.rate_limit_buckets (
  scope_hash text not null,
  window_start bigint not null,
  request_count integer not null check(request_count > 0),
  expires_at double precision not null,
  primary key(scope_hash, window_start)
);
create index if not exists idx_rate_limit_buckets_expires
  on agentgenia.rate_limit_buckets(expires_at);
alter table agentgenia.rate_limit_buckets enable row level security;
revoke all on agentgenia.rate_limit_buckets from public, anon, authenticated;
grant select, insert, update, delete on agentgenia.rate_limit_buckets to agentgenia_app;
insert into agentgenia.kv(k, v) values ('schema_version', '15')
on conflict (k) do update set v = excluded.v;
