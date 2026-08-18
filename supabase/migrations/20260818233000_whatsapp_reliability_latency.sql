-- WhatsApp reliability and cross-region run-reservation latency.
begin;

create index if not exists idx_whatsapp_messages_chat_order
  on agentgenia.whatsapp_messages(
    phone_number_id, wa_user_id, status, created_at, message_id
  );

-- Keep the complete credit reservation boundary inside PostgreSQL. The
-- previous Python transaction was correct but required one network round trip
-- per statement/grant, which dominated latency when Render and Supabase were
-- in different regions.
create or replace function agentgenia.reserve_agent_run(
  p_user_id text,
  p_idempotency_key text,
  p_model text,
  p_browser integer,
  p_max_credit_milli bigint,
  p_max_concurrent_runs integer,
  p_token_hash text,
  p_token_expires_at double precision,
  p_enforce boolean,
  p_five_hour_credit_milli bigint,
  p_seven_day_credit_milli bigint,
  p_run_id text,
  p_reservation_id text,
  p_ledger_id text,
  p_now double precision
)
returns table(outcome text, run jsonb, error_code text)
language plpgsql
security invoker
set search_path = agentgenia, public
as $$
declare
  v_existing agentgenia.agent_runs%rowtype;
  v_run agentgenia.agent_runs%rowtype;
  v_stale agentgenia.credit_reservations%rowtype;
  v_grant agentgenia.credit_grants%rowtype;
  v_active bigint;
  v_reserved bigint;
  v_charged bigint;
  v_available bigint;
  v_reserve bigint := case when p_enforce then p_max_credit_milli else 0 end;
  v_remaining bigint;
  v_allocated bigint;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_user_id, 0));

  -- Release only this user's expired reservations before enforcing limits.
  for v_stale in
    select * from agentgenia.credit_reservations
    where user_id=p_user_id and status='active' and expires_at<=p_now
    for update
  loop
    update agentgenia.credit_grants g
    set remaining_milli=g.remaining_milli+a.amount
    from (
      select grant_id,sum(allocated_milli)::bigint as amount
      from agentgenia.credit_reservation_allocations
      where reservation_id=v_stale.id group by grant_id
    ) a
    where g.id=a.grant_id;

    insert into agentgenia.credit_ledger(
      id,user_id,run_id,reservation_id,entry_type,amount_milli,
      idempotency_key,metadata_json,created_at
    ) values (
      'led_' || substr(md5(v_stale.id || ':expire'),1,16),
      p_user_id,v_stale.run_id,v_stale.id,'release',v_stale.reserved_milli,
      'expire:' || v_stale.id,'{"reason":"reservation_ttl"}',p_now
    ) on conflict(idempotency_key) do nothing;

    update agentgenia.credit_reservations
    set status='expired',settled_at=p_now where id=v_stale.id;
    update agentgenia.agent_runs
    set status='expired',finished_at=p_now,error_code='reservation_expired'
    where id=v_stale.run_id and status in ('reserved','running');
    update agentgenia.agent_run_tokens
    set revoked_at=p_now where run_id=v_stale.run_id and revoked_at is null;
  end loop;

  select * into v_existing from agentgenia.agent_runs
  where user_id=p_user_id and idempotency_key=p_idempotency_key;
  if found then
    if v_existing.status in ('failed','cancelled','expired','budget_exhausted') then
      update agentgenia.agent_runs
      set idempotency_key=p_idempotency_key || ':retired:' || v_existing.id
      where id=v_existing.id;
    else
      return query select 'duplicate',to_jsonb(v_existing),null::text;
      return;
    end if;
  end if;

  select count(*) into v_active from agentgenia.agent_runs
  where user_id=p_user_id and status in ('reserved','running');
  if v_active>=p_max_concurrent_runs then
    return query select 'error',null::jsonb,'credit_concurrency_limit';
    return;
  end if;

  if p_enforce then
    select coalesce(sum(reserved_milli),0) into v_reserved
    from agentgenia.credit_reservations
    where user_id=p_user_id and status='active';

    if p_five_hour_credit_milli is not null then
      select coalesce(sum(charged_credit_milli),0) into v_charged
      from agentgenia.agent_runs
      where user_id=p_user_id and created_at>=p_now-(5*3600);
      if v_charged+v_reserved+p_max_credit_milli>p_five_hour_credit_milli then
        return query select 'error',null::jsonb,'credit_5h_limit';
        return;
      end if;
    end if;

    if p_seven_day_credit_milli is not null then
      select coalesce(sum(charged_credit_milli),0) into v_charged
      from agentgenia.agent_runs
      where user_id=p_user_id and created_at>=p_now-(7*86400);
      if v_charged+v_reserved+p_max_credit_milli>p_seven_day_credit_milli then
        return query select 'error',null::jsonb,'credit_7d_limit';
        return;
      end if;
    end if;

    select coalesce(sum(remaining_milli),0) into v_available
    from agentgenia.credit_grants
    where user_id=p_user_id and remaining_milli>0 and starts_at<=p_now
      and (expires_at is null or expires_at>p_now);
    if v_available<p_max_credit_milli then
      return query select 'error',null::jsonb,'insufficient_credits';
      return;
    end if;
  end if;

  insert into agentgenia.agent_runs(
    id,user_id,idempotency_key,status,harness,model,browser,max_credit_milli,
    reserved_credit_milli,created_at,heartbeat_at
  ) values (
    p_run_id,p_user_id,p_idempotency_key,'reserved','pi',p_model,p_browser,
    p_max_credit_milli,v_reserve,p_now,p_now
  ) returning * into v_run;

  insert into agentgenia.credit_reservations(
    id,user_id,run_id,reserved_milli,status,expires_at,created_at
  ) values (
    p_reservation_id,p_user_id,p_run_id,v_reserve,'active',
    p_token_expires_at,p_now
  );

  v_remaining := v_reserve;
  if v_remaining>0 then
    for v_grant in
      select * from agentgenia.credit_grants
      where user_id=p_user_id and remaining_milli>0 and starts_at<=p_now
        and (expires_at is null or expires_at>p_now)
      order by case when expires_at is null then 1 else 0 end,expires_at,
        case source_type when 'subscription' then 0 when 'trial' then 1
          when 'promotion' then 2 when 'topup' then 3 else 4 end,created_at
      for update
    loop
      exit when v_remaining<=0;
      v_allocated := least(v_remaining,v_grant.remaining_milli);
      update agentgenia.credit_grants
      set remaining_milli=remaining_milli-v_allocated where id=v_grant.id;
      insert into agentgenia.credit_reservation_allocations(
        reservation_id,grant_id,allocated_milli
      ) values (p_reservation_id,v_grant.id,v_allocated);
      v_remaining := v_remaining-v_allocated;
    end loop;

    insert into agentgenia.credit_ledger(
      id,user_id,run_id,reservation_id,entry_type,amount_milli,
      idempotency_key,metadata_json,created_at
    ) values (
      p_ledger_id,p_user_id,p_run_id,p_reservation_id,'reserve',-v_reserve,
      'reserve:' || p_reservation_id,'{}',p_now
    );
  end if;

  insert into agentgenia.agent_run_tokens(
    token_hash,user_id,run_id,expires_at,created_at
  ) values (p_token_hash,p_user_id,p_run_id,p_token_expires_at,p_now);

  return query select 'inserted',to_jsonb(v_run),null::text;
end
$$;

revoke all on function agentgenia.reserve_agent_run(
  text,text,text,integer,bigint,integer,text,double precision,boolean,
  bigint,bigint,text,text,text,double precision
) from public, anon, authenticated, service_role;

do $$
begin
  if exists (select 1 from pg_roles where rolname='agentgenia_app') then
    grant execute on function agentgenia.reserve_agent_run(
      text,text,text,integer,bigint,integer,text,double precision,boolean,
      bigint,bigint,text,text,text,double precision
    ) to agentgenia_app;
  end if;
end
$$;

insert into agentgenia.kv(k,v) values ('schema_version','23')
on conflict(k) do update set v=excluded.v;

commit;
