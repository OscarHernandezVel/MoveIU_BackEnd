# Referencia de alta disponibilidad y copias de seguridad

Este documento es una **referencia de diseño**, no un despliegue probado en este repositorio: la
conmutación por error real depende de su infraestructura (nube, Kubernetes o servidores propios). El servicio ya
está preparado para ella (sondas, reintentos, pool que descarta conexiones muertas, estado compartido).
Si usa PostgreSQL y Redis **gestionados** (Cloud SQL, RDS, Azure, Timescale Cloud, ElastiCache, Redis Cloud…), la
mayor parte de lo que sigue lo ofrece el proveedor: verifique que incluya la extensión TimescaleDB.

## Topología mínima recomendada

```text
                 balanceador (TLS)
              ┌──────┴──────┐
        core-service    core-service   (≥ 2 réplicas, sin estado local)
              │   ╲       ╱   │
   ┌──────────┴────╲─────╱────┴──────────┐
   │ PostgreSQL+Timescale                │  Redis
   │ primario ⇄ réplica (Patroni/gestionado)│  primario ⇄ réplica  +  3 × Sentinel
   │ + archivado WAL (pgBackRest)        │  AOF everysec, noeviction
   └─────────────────────────────────────┘
```

## PostgreSQL / TimescaleDB

1. **Replicación en streaming** con al menos una réplica en otra zona de disponibilidad. Para no perder
   ninguna confirmación use `synchronous_commit = on` con una réplica síncrona (más latencia de escritura); con
   réplica asíncrona el riesgo es perder los últimos milisegundos en una conmutación.
2. **Conmutación automática** con Patroni (etcd/Consul) o el servicio gestionado, detrás de un único nombre DNS o
   un proxy (PgBouncer, HAProxy) para que `CORE_DATABASE_URL` no cambie. `Database` valida cada conexión al
   entregarla, así que tras la conmutación descarta los sockets muertos y reconecta sola.
3. **Copias**: copia base diaria + archivado continuo de WAL con `pgBackRest` o `wal-g` a almacenamiento de
   objetos cifrado (PITR: volver a cualquier segundo). Retención sugerida: 14 días.
4. **Simulacro de restauración** al menos una vez al trimestre: una copia que nunca se restauró no es una copia.
5. Con TimescaleDB, la compresión y la retención reducen el tamaño de lo que hay que copiar; el agregado
   continuo `telemetry_5min` se conserva 365 días.
6. Aplique `deploy/postgres/01-roles.sql` y luego (tras la primera migración) `02-reader-grants.sql`. Exija TLS
   en `pg_hba.conf` (`hostssl`) y use `sslmode=verify-full` en las URL.

## Redis

1. Un primario, **una o más réplicas** y **tres Sentinel** en máquinas distintas. Configure
   `CORE_REDIS_SENTINELS=h1:26379,h2:26379,h3:26379`, `CORE_REDIS_SENTINEL_MASTER`, `CORE_REDIS_TLS_CA`.
2. `deploy/redis/redis.conf` ya fija `appendonly yes`, `noeviction` y `min-replicas-to-write 1`:
   el primario deja de aceptar escrituras si queda aislado sin réplicas (evita dos primarios que divergen).
3. Los datos de Redis son reconstruibles a partir de PostgreSQL y de las siguientes lecturas de los buses, por lo
   que no hace falta PITR; mantenga una copia del RDB/AOF solo para acelerar el arranque en frío.
4. Las sesiones (tokens de refresco) viven en Redis: si se perdiera por completo, los usuarios deberán iniciar
   sesión de nuevo (los invitados se recrean solos).

## Servicio

* ≥ 2 réplicas; actualizaciones progresivas (*rolling*). Las migraciones son seguras con arranques simultáneos.
* Sonda de vida: `GET /api/v1/health/live`. Sonda de preparación: `GET /api/v1/health/ready` (503 si
  PostgreSQL o Redis no responden: el balanceador deja de enviar tráfico a esa réplica).
* Límites de recursos y `CORE_DB_POOL_MAX` × réplicas < `max_connections` de PostgreSQL (con margen para
  migraciones y administración) o use PgBouncer en modo *transaction*.
* Alertas de monitorización sugeridas: latencia p95 de `/internal/buses/*/telemetry`, retraso de replicación,
  memoria de Redis > 80 %, estado del circuito de la IA, tamaño del pool (`Database.pool_stats`).

## Objetivos orientativos (a validar con una prueba de fallo real)

| Escenario | RTO esperado | RPO |
|---|---|---|
| Cae una réplica del servicio | 0 (el balanceador la retira) | 0 |
| Conmutación de PostgreSQL | decenas de segundos | 0 (síncrona) / milisegundos (asíncrona) |
| Conmutación de Redis | ~10–30 s | ≤ 1 s de escrituras (AOF) |
| Desastre de zona completa | minutos a horas (restauración PITR) | segundos de WAL |
