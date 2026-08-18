begin;

create table if not exists agentgenia.desktop_runtime_devices (
  user_id             text not null references agentgenia.users(id) on delete cascade,
  device_id_hash      text not null,
  platform            text not null,
  app_version         text not null default '',
  capabilities_json   text not null default '{}',
  last_seen_at        double precision not null,
  lease_expires_at    double precision not null,
  created_at          double precision not null,
  updated_at          double precision not null,
  primary key(user_id, device_id_hash)
);

create table if not exists agentgenia.desktop_runtime_jobs (
  id                    text primary key,
  user_id               text not null references agentgenia.users(id) on delete cascade,
  run_id                text not null unique references agentgenia.agent_runs(id) on delete cascade,
  bot_id                text,
  job_kind              text not null check(job_kind in ('browser','computer')),
  status                text not null default 'pending'
    check(status in ('pending','claimed','succeeded','failed','expired','cancelled')),
  payload_enc           bytea not null,
  key_id                text not null,
  key_version           integer not null default 1,
  claimed_device_hash   text,
  claim_expires_at      double precision,
  result_json           text,
  error_code            text,
  error_message         text,
  expires_at            double precision not null,
  created_at            double precision not null,
  updated_at            double precision not null,
  finished_at           double precision
);

create index if not exists idx_desktop_runtime_devices_online
  on agentgenia.desktop_runtime_devices(user_id, lease_expires_at);
create index if not exists idx_desktop_runtime_jobs_pending
  on agentgenia.desktop_runtime_jobs(user_id, status, created_at);
create index if not exists idx_desktop_runtime_jobs_expiry
  on agentgenia.desktop_runtime_jobs(status, expires_at);

alter table agentgenia.desktop_runtime_devices enable row level security;
alter table agentgenia.desktop_runtime_jobs enable row level security;
revoke all on agentgenia.desktop_runtime_devices from public, anon, authenticated;
revoke all on agentgenia.desktop_runtime_jobs from public, anon, authenticated;

insert into agentgenia.kv(k, v) values ('schema_version', '21')
on conflict(k) do update set v=excluded.v;

commit;
