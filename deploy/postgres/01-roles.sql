-- =====================================================================================
-- Roles con mínimo privilegio para el core-service.
-- Ejecutar UNA vez, como superusuario, conectado a la base de datos del servicio.
--
-- Sustituya los marcadores __*_PASSWORD__ por contraseñas largas y aleatorias (o use
-- autenticación por certificado). Nunca versione las contraseñas reales.
--
--   core_migrator  dueño de las tablas: crea/altera el esquema y las políticas de TimescaleDB.
--                  Solo lo usan las migraciones (CORE_DATABASE_MIGRATION_URL), no el tráfico normal.
--   core_app       lo que usa el servicio en ejecución: SELECT/INSERT/UPDATE/DELETE. No puede crear,
--                  alterar ni borrar tablas, ni truncar, ni crear roles.
--   core_reader    solo lectura y solo datos de flota (ver 02-reader-grants.sql). No ve usuarios,
--                  dispositivos, tokens ni alertas: sirve para analítica/IA sin datos personales.
-- =====================================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'core_migrator') THEN
        CREATE ROLE core_migrator LOGIN PASSWORD '__MIGRATOR_PASSWORD__'
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'core_app') THEN
        CREATE ROLE core_app LOGIN PASSWORD '__APP_PASSWORD__'
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION CONNECTION LIMIT 100;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'core_reader') THEN
        CREATE ROLE core_reader LOGIN PASSWORD '__READER_PASSWORD__'
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION CONNECTION LIMIT 10;
    END IF;
END
$$;

-- Nadie entra a la base de datos si no se le ha concedido.
DO $$
BEGIN
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO core_migrator, core_app, core_reader', current_database());
END
$$;

-- El esquema public deja de ser de todos.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO core_migrator;
GRANT USAGE ON SCHEMA public TO core_app, core_reader;

-- Todo lo que cree core_migrator en el futuro queda accesible al servicio (solo datos, nunca DDL).
ALTER DEFAULT PRIVILEGES FOR ROLE core_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO core_app;
ALTER DEFAULT PRIVILEGES FOR ROLE core_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO core_app;

-- Límites de seguridad por rol (el servicio también los fija por conexión).
ALTER ROLE core_app SET statement_timeout = '10s';
ALTER ROLE core_app SET idle_in_transaction_session_timeout = '15s';
ALTER ROLE core_reader SET statement_timeout = '30s';
ALTER ROLE core_reader SET default_transaction_read_only = on;
