-- Se ejecuta una sola vez, al crear el volumen de datos por primera vez.
-- Activa las extensiones y crea la base adicional de las pruebas (pytest crea un esquema por prueba dentro de ella).
\connect core
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS postgis;
\connect postgres
CREATE DATABASE core_test OWNER core;
\connect core_test
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS postgis;
