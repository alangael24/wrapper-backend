-- Production audit hardening.
-- Business is a first-class paid tier in the API and Stripe catalog. Replace
-- the legacy checks before accepting another Business webhook.
do $$
declare
  constraint_name text;
begin
  for constraint_name in
    select conname
    from pg_constraint
    where conrelid = 'agentgenia.users'::regclass
      and contype = 'c'
      and pg_get_constraintdef(oid) ilike '%tier%'
  loop
    execute format('alter table agentgenia.users drop constraint %I', constraint_name);
  end loop;

  for constraint_name in
    select conname
    from pg_constraint
    where conrelid = 'agentgenia.billing_subscriptions'::regclass
      and contype = 'c'
      and pg_get_constraintdef(oid) ilike '%tier%'
  loop
    execute format(
      'alter table agentgenia.billing_subscriptions drop constraint %I',
      constraint_name
    );
  end loop;
end
$$;

alter table agentgenia.users
  add constraint users_tier_valid
  check (tier in ('free', 'basic', 'pro', 'business'));

alter table agentgenia.billing_subscriptions
  add constraint billing_subscriptions_tier_valid
  check (tier in ('basic', 'pro', 'business'));

create index if not exists idx_billing_subscription_authoritative
  on agentgenia.billing_subscriptions(
    user_id,
    (case when status in ('active', 'trialing', 'past_due') then 0 else 1 end),
    last_stripe_event_created desc,
    updated_at desc
  );
create index if not exists idx_stripe_events_processed
  on agentgenia.stripe_events(processed_at);
create index if not exists idx_account_sessions_expiry
  on agentgenia.account_sessions(refresh_expires_at, revoked_at);
create index if not exists idx_agent_runs_finished
  on agentgenia.agent_runs(finished_at)
  where finished_at is not null;

insert into agentgenia.kv(k, v) values ('schema_version', '14')
on conflict (k) do update set v = excluded.v;
