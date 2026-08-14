-- Durable official WhatsApp channel: one personal identity per Agent Genia
-- account, one-time link codes, and an idempotent webhook inbox.
create table if not exists agentgenia.whatsapp_link_codes (
  code_hash text primary key,
  user_id text not null references agentgenia.users(id) on delete cascade,
  expires_at double precision not null,
  consumed_at double precision,
  created_at double precision not null
);

create table if not exists agentgenia.whatsapp_links (
  wa_user_id text primary key,
  user_id text unique not null references agentgenia.users(id) on delete cascade,
  phone_number_id text not null,
  display_name text not null default '',
  active_bot_id text,
  created_at double precision not null,
  updated_at double precision not null
);

create table if not exists agentgenia.whatsapp_messages (
  message_id text primary key,
  user_id text references agentgenia.users(id) on delete cascade,
  phone_number_id text not null,
  wa_user_id text not null,
  message_type text not null,
  text text not null default '',
  payload_json text not null,
  status text not null default 'pending'
    check (status in ('pending', 'processing', 'succeeded', 'ignored', 'failed')),
  attempts integer not null default 0 check (attempts >= 0),
  next_attempt_at double precision not null default 0,
  result_text text not null default '',
  outbound_message_id text,
  last_error text not null default '',
  created_at double precision not null,
  updated_at double precision not null
);

create index if not exists idx_whatsapp_link_codes_user
  on agentgenia.whatsapp_link_codes(user_id, expires_at);
create unique index if not exists uniq_whatsapp_link_code_user
  on agentgenia.whatsapp_link_codes(user_id);
create index if not exists idx_whatsapp_links_user
  on agentgenia.whatsapp_links(user_id);
create index if not exists idx_whatsapp_messages_pending
  on agentgenia.whatsapp_messages(status, next_attempt_at, created_at);
create index if not exists idx_whatsapp_messages_user
  on agentgenia.whatsapp_messages(user_id, created_at);

-- These tables are intentionally private. The backend connects with the
-- BYPASSRLS agentgenia_app role; browser-facing roles receive no policies.
alter table agentgenia.whatsapp_link_codes enable row level security;
alter table agentgenia.whatsapp_links enable row level security;
alter table agentgenia.whatsapp_messages enable row level security;

revoke all on agentgenia.whatsapp_link_codes from public, anon, authenticated, service_role;
revoke all on agentgenia.whatsapp_links from public, anon, authenticated, service_role;
revoke all on agentgenia.whatsapp_messages from public, anon, authenticated, service_role;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'agentgenia_app') then
    grant select, insert, update, delete
      on agentgenia.whatsapp_link_codes,
         agentgenia.whatsapp_links,
         agentgenia.whatsapp_messages
      to agentgenia_app;
  end if;
end
$$;

insert into agentgenia.kv(k, v) values ('schema_version', '17')
on conflict (k) do update set v = excluded.v;
