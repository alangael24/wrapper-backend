-- The delivery state is persisted before contacting Meta so that a network
-- timeout can never cause an automatic duplicate reply. Schema v17 predates
-- that state and rejects the otherwise valid transition at runtime.
begin;

alter table agentgenia.whatsapp_messages
  drop constraint if exists whatsapp_messages_status_check;

alter table agentgenia.whatsapp_messages
  add constraint whatsapp_messages_status_check
  check (status in (
    'pending',
    'processing',
    'sending',
    'succeeded',
    'ignored',
    'failed'
  ));

insert into agentgenia.kv(k, v) values ('schema_version', '22')
on conflict (k) do update set v = excluded.v;

commit;
