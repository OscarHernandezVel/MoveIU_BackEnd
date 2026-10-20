#!/bin/bash
# Se ejecuta UNA vez, al crear el volumen de datos de PostgreSQL (docker-entrypoint-initdb.d).
# 1) Activa TimescaleDB y PostGIS como superusuario (el rol migrador no puede crear extensiones).
# 2) Crea los roles core_migrator / core_app / core_reader con las contraseñas de los secretos.
set -euo pipefail

read -r APP_PW < /run/secrets/pg_app_password
read -r MIGRATOR_PW < /run/secrets/pg_migrator_password
read -r READER_PW < /run/secrets/pg_reader_password

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS postgis;
SQL

# Las contraseñas son hexadecimales (init_secrets.py), seguras para sed.
sed -e "s/__APP_PASSWORD__/${APP_PW}/" \
    -e "s/__MIGRATOR_PASSWORD__/${MIGRATOR_PW}/" \
    -e "s/__READER_PASSWORD__/${READER_PW}/" /sql/01-roles.sql \
  | psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"
