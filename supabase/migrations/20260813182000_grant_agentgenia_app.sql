-- The production backend connects through this dedicated server-only role.
-- Keep browser-facing roles revoked while granting the application the least
-- privileges it needs to manage its private schema.
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'agentgenia_app') then
    grant usage on schema agentgenia to agentgenia_app;

    grant select, insert, update, delete
      on all tables in schema agentgenia
      to agentgenia_app;

    grant usage, select, update
      on all sequences in schema agentgenia
      to agentgenia_app;

    alter default privileges for role postgres in schema agentgenia
      grant select, insert, update, delete on tables to agentgenia_app;

    alter default privileges for role postgres in schema agentgenia
      grant usage, select, update on sequences to agentgenia_app;
  end if;
end
$$;
