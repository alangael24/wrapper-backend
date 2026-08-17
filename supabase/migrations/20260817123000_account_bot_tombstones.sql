-- Durable server-side deletion memory prevents stale offline devices from
-- resurrecting a bot after the bounded client tombstone list rotates.
create table if not exists agentgenia.account_bot_tombstones (
  user_id    text not null references agentgenia.users(id) on delete cascade,
  bot_id     text not null,
  deleted_at double precision not null,
  primary key (user_id, bot_id)
);

create index if not exists idx_account_bot_tombstones_deleted
  on agentgenia.account_bot_tombstones(user_id, deleted_at);

alter table agentgenia.account_bot_tombstones enable row level security;
revoke all on agentgenia.account_bot_tombstones
  from public, anon, authenticated, service_role;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'agentgenia_app') then
    grant select, insert, update, delete
      on agentgenia.account_bot_tombstones to agentgenia_app;
  end if;
end
$$;

insert into agentgenia.kv(k, v) values ('schema_version', '19')
on conflict (k) do update set v = excluded.v;
