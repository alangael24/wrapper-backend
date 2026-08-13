-- Agent Genia now uses one server-owned DeepSeek account. Product users no
-- longer receive provider credentials and usage is attributed directly to the
-- Agent Genia user.

alter table agentgenia.usage_events
  alter column subscription_id drop not null;

update agentgenia.users
set subscription_id = null
where subscription_id is not null;

update agentgenia.go_subscriptions
set status = 'revoked', assigned_user_id = null
where status <> 'revoked' or assigned_user_id is not null;

insert into agentgenia.kv(k, v) values ('schema_version', '10')
on conflict (k) do update set v = excluded.v;
