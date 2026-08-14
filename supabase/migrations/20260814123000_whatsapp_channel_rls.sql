-- Production follow-up for deployments that applied schema v17 before the
-- WhatsApp tables were explicitly switched to deny-by-default RLS.
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
