-- Preserve Stripe's event time so entitlement updates cannot move backward
-- when webhook delivery is delayed or retried out of order.
alter table agentgenia.billing_subscriptions
  add column if not exists last_stripe_event_created bigint not null default 0;

alter table agentgenia.stripe_events
  add column if not exists stripe_event_created bigint not null default 0;

do $$
begin
  if not exists (
    select 1
    from pg_constraint
    where conname = 'billing_subscriptions_last_event_created_nonnegative'
      and conrelid = 'agentgenia.billing_subscriptions'::regclass
  ) then
    alter table agentgenia.billing_subscriptions
      add constraint billing_subscriptions_last_event_created_nonnegative
      check (last_stripe_event_created >= 0);
  end if;

  if not exists (
    select 1
    from pg_constraint
    where conname = 'stripe_events_event_created_nonnegative'
      and conrelid = 'agentgenia.stripe_events'::regclass
  ) then
    alter table agentgenia.stripe_events
      add constraint stripe_events_event_created_nonnegative
      check (stripe_event_created >= 0);
  end if;
end $$;

insert into agentgenia.kv(k, v) values ('schema_version', '7')
on conflict (k) do update set v = excluded.v;
