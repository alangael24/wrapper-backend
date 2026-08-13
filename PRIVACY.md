# Aviso de privacidad de Agent Genia

**Vigente desde: 12 de agosto de 2026**

Agent Genia ofrece asistentes de IA, conectores y computadoras de agentes. Este
aviso explica qué datos tratamos al usar nuestras aplicaciones y servicios.

## Datos que tratamos

- Identidad de cuenta: nombre, email, identificador y, cuando esté disponible,
  foto que entrega Google o Apple.
- Uso del producto: bots, instrucciones, mensajes, workflows, preferencias,
  conectores elegidos y metadatos de ejecución.
- Contenido solicitado por el usuario: texto, archivos, capturas y resultados
  necesarios para ejecutar una tarea.
- Suscripción: customer/subscription IDs, plan, estado y eventos de pago. Stripe
  procesa los datos completos de tarjeta; Agent Genia no los almacena.
- Datos técnicos y de seguridad: dispositivo, versión, dirección IP, request ID,
  timestamps, errores, consumo y eventos necesarios para prevenir abuso.
- Datos de servicios conectados: únicamente los recursos que el usuario pide al
  agente consultar o modificar conforme a los permisos otorgados.

No vendemos datos personales ni los usamos para publicidad conductual. Las apps
móviles declaran que no hacen tracking entre aplicaciones o sitios de terceros.

## Para qué se usan

Usamos los datos para autenticar, ejecutar tareas, mantener bots y computadoras,
autorizar conectores, facturar, dar soporte, proteger el servicio, cumplir la ley
y mejorar confiabilidad. No entrenamos modelos públicos con contenido privado de
usuarios salvo consentimiento separado y explícito.

## Proveedores

Según las funciones habilitadas, los datos pueden ser procesados por Google
(login), Stripe (pagos), Supabase/PostgreSQL (persistencia), Render
(infraestructura), Composio y cada proveedor conectado (OAuth/herramientas),
Daytona (computadoras aisladas), y los proveedores de modelos configurados por
Agent Genia. Cada proveedor procesa información bajo sus propios términos y las
instrucciones técnicas necesarias para prestar el servicio.

## Retención y seguridad

Conservamos datos mientras la cuenta esté activa y durante el tiempo razonable
para seguridad, facturación, disputas y obligaciones legales. Los grants internos
de conectores son efímeros; los secretos se cifran; los tokens de sesión se
guardan hasheados o en almacenamiento seguro del dispositivo. Al eliminar una
cuenta, las apps borran su estado local y rotan el identificador aleatorio de la
instalación. Los backups pueden persistir temporalmente después de una eliminación.

## Controles del usuario

Puedes desconectar proveedores desde Plugins y cerrar sesiones. Para eliminar tu
cuenta y sus datos, usa **Cuenta → Eliminar cuenta y datos** en Electron, iOS o
Android.
Si ya no tienes acceso a la app, visita
`https://agentgenia-api.onrender.com/account-deletion`. Para acceso, corrección o
exportación escribe a `privacy@agentgenia.com`. También puedes revocar permisos
desde la cuenta del proveedor conectado. Podemos pedir verificación de identidad
antes de responder.

## Menores y transferencias

El servicio no está dirigido a menores de 13 años ni a una edad inferior exigida
por la ley local. Los proveedores pueden procesar datos en otros países con las
salvaguardas disponibles para la transferencia correspondiente.

## Cambios y contacto

Publicaremos cambios materiales y actualizaremos la fecha de vigencia. Para
privacidad: `privacy@agentgenia.com`. Para seguridad:
`security@agentgenia.com`. Operador del servicio: Agent Genia.
