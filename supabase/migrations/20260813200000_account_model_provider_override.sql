-- DeepSeek remains the product default. A server operator may opt a specific
-- account into OpenCode using an encrypted credential that already lives in
-- the private provider-key table. There is intentionally no client/BYOK API.

alter table agentgenia.users
  add column if not exists model_provider_override text;

alter table agentgenia.users
  add column if not exists unlimited_usage integer not null default 0;

alter table agentgenia.users
  drop constraint if exists users_model_provider_override_check;
alter table agentgenia.users
  add constraint users_model_provider_override_check
  check (
    model_provider_override is null
    or model_provider_override = 'opencode'
  );

alter table agentgenia.users
  drop constraint if exists users_unlimited_usage_check;
alter table agentgenia.users
  add constraint users_unlimited_usage_check
  check (unlimited_usage in (0, 1));

-- These fields stay private even if the schema is accidentally exposed.
revoke all on agentgenia.users from public, anon, authenticated;

insert into agentgenia.kv(k, v) values ('schema_version', '12')
on conflict (k) do update set v = excluded.v;
