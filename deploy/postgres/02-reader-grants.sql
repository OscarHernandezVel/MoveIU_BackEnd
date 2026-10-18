-- =====================================================================================
-- Permisos del rol de solo lectura (analítica / IA). Ejecutar como core_migrator (o superusuario)
-- DESPUÉS de aplicar las migraciones del servicio.
--
-- Solo se concede lo que es dato de flota. Quedan fuera, por diseño: users, devices (correo,
-- teléfono, token push, hash de contraseña), alerts (qué dispositivo espera en qué parada) y la
-- columna auditor_id de audit_samples. Un error en la aplicación o en el modelo de IA no puede
-- sacar datos personales por este rol porque el motor de base de datos se lo impide.
-- =====================================================================================

GRANT SELECT ON telemetry_history, telemetry_5min, routes, stops, route_stops, buses TO core_reader;

GRANT SELECT (finding_id, bus_id, kind, severity, description, source, status, created_at)
    ON technical_findings TO core_reader;

GRANT SELECT (sample_id, bus_id, stop_id, manual_seated, manual_standing, model_seated, model_standing,
              doors_total, confidence, model_version, sampled_at)
    ON audit_samples TO core_reader;
