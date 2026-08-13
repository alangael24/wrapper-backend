-- Integer credit wallet, per-run reservations and ephemeral model tokens.

alter table agentgenia.usage_events add column if not exists run_id text;
alter table agentgenia.usage_events
  add column if not exists estimated_cost_microusd bigint not null default 0;
update agentgenia.usage_events
set estimated_cost_microusd = round(estimated_cost_usd * 1000000)::bigint
where estimated_cost_microusd = 0 and estimated_cost_usd > 0;

create table if not exists agentgenia.agent_runs (
  id text primary key,
  user_id text not null references agentgenia.users(id) on delete cascade,
  idempotency_key text not null,
  status text not null,
  harness text not null default 'pi',
  model text,
  browser integer not null default 0,
  max_credit_milli bigint not null,
  reserved_credit_milli bigint not null default 0,
  charged_credit_milli bigint not null default 0,
  llm_cost_microusd bigint not null default 0,
  extra_cost_microusd bigint not null default 0,
  duration_seconds double precision,
  error_code text,
  warnings_json text not null default '[]',
  created_at double precision not null,
  started_at double precision,
  finished_at double precision,
  heartbeat_at double precision,
  check(max_credit_milli > 0),
  check(reserved_credit_milli >= 0),
  check(charged_credit_milli >= 0),
  check(charged_credit_milli <= max_credit_milli),
  check(llm_cost_microusd >= 0),
  check(extra_cost_microusd >= 0),
  unique(user_id, idempotency_key)
);

create table if not exists agentgenia.credit_grants (
  id text primary key,
  user_id text not null references agentgenia.users(id) on delete cascade,
  source_type text not null,
  source_key text not null unique,
  original_milli bigint not null check(original_milli > 0),
  remaining_milli bigint not null check(remaining_milli >= 0),
  starts_at double precision not null,
  expires_at double precision,
  metadata_json text not null default '{}',
  created_at double precision not null,
  check(remaining_milli <= original_milli)
);

create table if not exists agentgenia.credit_reservations (
  id text primary key,
  user_id text not null references agentgenia.users(id) on delete cascade,
  run_id text not null unique references agentgenia.agent_runs(id) on delete cascade,
  reserved_milli bigint not null check(reserved_milli >= 0),
  charged_milli bigint not null default 0 check(charged_milli >= 0),
  status text not null,
  expires_at double precision not null,
  created_at double precision not null,
  settled_at double precision,
  check(charged_milli <= reserved_milli)
);

create table if not exists agentgenia.credit_reservation_allocations (
  reservation_id text not null references agentgenia.credit_reservations(id) on delete cascade,
  grant_id text not null references agentgenia.credit_grants(id) on delete cascade,
  allocated_milli bigint not null check(allocated_milli > 0),
  primary key(reservation_id, grant_id)
);

create table if not exists agentgenia.credit_ledger (
  id text primary key,
  user_id text not null references agentgenia.users(id) on delete cascade,
  run_id text,
  grant_id text,
  reservation_id text,
  entry_type text not null,
  amount_milli bigint not null,
  idempotency_key text not null unique,
  metadata_json text not null default '{}',
  created_at double precision not null
);

create table if not exists agentgenia.agent_run_tokens (
  token_hash text primary key,
  user_id text not null references agentgenia.users(id) on delete cascade,
  run_id text not null unique references agentgenia.agent_runs(id) on delete cascade,
  expires_at double precision not null,
  revoked_at double precision,
  created_at double precision not null
);

create index if not exists idx_usage_run
  on agentgenia.usage_events(run_id, created_at);
create index if not exists idx_agent_runs_user_status
  on agentgenia.agent_runs(user_id, status);
create index if not exists idx_credit_grants_user_expiry
  on agentgenia.credit_grants(user_id, expires_at, created_at);
create index if not exists idx_credit_ledger_user_created
  on agentgenia.credit_ledger(user_id, created_at);
create index if not exists idx_credit_reservations_user
  on agentgenia.credit_reservations(user_id);
create index if not exists idx_credit_allocations_grant
  on agentgenia.credit_reservation_allocations(grant_id);
create index if not exists idx_agent_run_tokens_user
  on agentgenia.agent_run_tokens(user_id);

-- Keep server-owned billing state inaccessible even if the private schema is
-- accidentally exposed through the Data API.
alter table agentgenia.agent_runs enable row level security;
alter table agentgenia.credit_grants enable row level security;
alter table agentgenia.credit_reservations enable row level security;
alter table agentgenia.credit_reservation_allocations enable row level security;
alter table agentgenia.credit_ledger enable row level security;
alter table agentgenia.agent_run_tokens enable row level security;

revoke all on agentgenia.agent_runs from public, anon, authenticated;
revoke all on agentgenia.credit_grants from public, anon, authenticated;
revoke all on agentgenia.credit_reservations from public, anon, authenticated;
revoke all on agentgenia.credit_reservation_allocations from public, anon, authenticated;
revoke all on agentgenia.credit_ledger from public, anon, authenticated;
revoke all on agentgenia.agent_run_tokens from public, anon, authenticated;

insert into agentgenia.kv(k, v) values ('schema_version', '11')
on conflict (k) do update set v = excluded.v;
