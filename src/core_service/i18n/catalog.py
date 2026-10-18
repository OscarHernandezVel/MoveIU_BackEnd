"""User-facing message catalog (Spanish by default, English as a second locale).

Identifiers in code are English; the *content* shown to passengers is product copy and
is localised here. Messages use plain language: no jargon and no technical codes
(see the UI/UX principles of the project document).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

DEFAULT_LOCALE = "es"

_MESSAGES: dict[str, dict[str, str]] = {
    "es": {
        # ---- generic errors
        "validation_error": "Revisa los datos enviados.",
        "invalid_coordinates": "Las coordenadas no son válidas.",
        "invalid_capacity": "La capacidad del bus no es válida.",
        "invalid_telemetry": "El reporte del bus no es válido.",
        "not_found": "No encontramos lo que buscas.",
        "conflict": "Los datos ya existen o entran en conflicto con otros.",
        "authentication_failed": "No pudimos verificar tu identidad.",
        "forbidden": "No tienes permiso para hacer esto.",
        "rate_limited": "Estás haciendo muchas consultas seguidas. Espera un momento.",
        "service_unavailable": "El servicio no está disponible por ahora. Intenta de nuevo en unos segundos.",
        "internal_error": "Tuvimos un problema. Intenta de nuevo en unos segundos.",
        "payload_too_large": "El archivo o mensaje enviado es demasiado grande.",
        "privacy_violation": "La solicitud contiene datos personales que no pueden procesarse.",
        # ---- catalog / buses
        "route_not_found": "No encontramos la ruta {route_id}.",
        "stop_not_found": "No encontramos la parada {stop_id}.",
        "stop_not_in_route": "Esa parada no está en la ruta {route_id} en ese sentido.",
        "bus_not_found": "No encontramos el bus {bus_id}.",
        "invalid_bus_id": "El identificador del bus no es válido.",
        "bus_not_on_route": "El bus {bus_id} no está prestando servicio en la ruta {route_id}.",
        "too_many_routes": "Puedes consultar entre 1 y {max_routes} rutas a la vez.",
        "route_invalid_stops": "Las paradas de la ruta {route_id} no siguen el trazado.",
        # ---- authentication
        "invalid_credentials": "El correo o la contraseña no coinciden.",
        "account_locked": "Por seguridad, espera {minutes} minutos antes de intentar de nuevo.",
        "session_expired": "Tu sesión terminó. Vuelve a ingresar.",
        "email_taken": "Ese correo ya está registrado. ¿Quieres iniciar sesión?",
        "device_not_registered": "Este celular no está registrado. Abre la app de nuevo para registrarlo.",
        "password_policy": "Revisa tu contraseña.",
        "password_too_short": "La contraseña necesita al menos {min_length} caracteres.",
        "password_too_long": "La contraseña no puede tener más de {max_length} caracteres.",
        "password_no_digit": "Parece que te faltó un número, revísalo por favor.",
        "password_no_uppercase": "Agrega al menos una letra mayúscula.",
        # ---- alerts
        "alert_limit_reached": "Ya tienes el máximo de alertas activas. Cancela una para crear otra.",
        "alert_not_found": "No encontramos esa alerta activa.",
        "alert_not_deliverable": "No pudimos entregar la alerta por ningún canal.",
        # ---- audits
        "audit_bus_silent": "El bus {bus_id} no está reportando ahora.",
        "audit_no_data": "No hay datos del bus {bus_id} cerca de esa hora.",
        "audit_invalid_range": "La fecha final no puede ser anterior a la inicial.",
        # ---- AI
        "ai_disabled": "El análisis con inteligencia artificial no está habilitado.",
        "ai_invalid_request": "La consulta de análisis no es válida.",
        # ---- occupancy state labels
        "occupancy.state.available_seats": "Asientos disponibles",
        "occupancy.state.standing_only": "Solo de pie",
        "occupancy.state.full": "Lleno",
        "occupancy.state.overcrowded": "Sobrecupo",
        "occupancy.state.no_data": "Datos no disponibles",
        # ---- screen alerts
        "alert.proximity.distance.one": "a 1 parada",
        "alert.proximity.distance.many": "a {stops} paradas",
        "alert.proximity.title": "Tu {route_id} está {distance}",
        "alert.proximity.body": "Bus {bus_id} hacia {stop_name}: {state_label} ({occupancy_pct}%).",
        "alert.proximity.next_bus": " El siguiente: {state_label}, a {stops} paradas.",
        "alert.proximity.a11y": (
            "Alerta: tu bus {route_id}, número {bus_id}, está {distance}. "
            "Estado: {state_label}, {occupancy_pct} por ciento de ocupación."
        ),
        "alert.occupancy_warning.title": "Tu bus {route_id} viene lleno",
        "alert.occupancy_warning.body": "El bus {bus_id} ahora está en estado «{state_label}» ({occupancy_pct}%).",
        "alert.occupancy_warning.alternative": " El siguiente ({next_bus_id}) trae asientos libres.",
        "alert.occupancy_warning.a11y": (
            "Aviso: el bus {route_id} número {bus_id} viene lleno. Estado: {state_label}, "
            "{occupancy_pct} por ciento de ocupación."
        ),
        "alert.bus_lost.title": "Perdimos la señal de tu bus",
        "alert.bus_lost.body": "No recibimos datos del bus {bus_id} hace más de {seconds} segundos. Tu alerta sigue activa.",
        "alert.bus_lost.a11y": "Aviso: perdimos la señal del bus {bus_id}. Tu alerta sigue activa.",
        "alert.expired.title": "Tu alerta expiró",
        "alert.expired.body": (
            "Pasaron {minutes} minutos y tu bus {route_id} no llegó a {stop_name}. Puedes crear otra alerta."
        ),
        "alert.expired.a11y": "Tu alerta de la ruta {route_id} expiró. Puedes crear otra.",
        "ai_unavailable": "El análisis con inteligencia artificial no está disponible ahora.",
        # ---- findings (text for the technical team)
        "finding.count_mismatch": "Bus {bus_id}: las cámaras y las puertas difieren en más de {threshold} personas por más de {seconds} s.",
        "finding.low_confidence": "Bus {bus_id}: la confianza del conteo bajó de {min_confidence}.",
        "finding.silent_bus": "El bus {bus_id} de la ruta {route_id} no reporta hace más de {seconds} segundos.",
        "finding.digest.subject": "[Capacidad buses] Resumen diario de hallazgos ({count})",
        "finding.alert.subject": "[Capacidad buses] {description}",
        # ---- weekly audit report (team e-mail)
        "report.audit.subject": "[Capacidad buses] Reporte semanal de precisión ({start} a {end})",
        "report.audit.summary": "Muestras: {samples} | Error promedio: {error}%",
        "report.audit.recalibrate": "Buses para recalibrar (error > {threshold}% en dos auditorías seguidas): {buses}",
        "report.audit.none": "ninguno",
        "report.audit.by_bus": "Por bus:",
        "report.audit.by_model": "Por versión del modelo:",
        "report.audit.line": "  {label}: {samples} muestras, error medio {abs_error} personas ({pct_error}%)",
    },
    "en": {
        "validation_error": "Please check the submitted data.",
        "invalid_coordinates": "The coordinates are not valid.",
        "invalid_capacity": "The bus capacity is not valid.",
        "invalid_telemetry": "The bus report is not valid.",
        "not_found": "We could not find what you are looking for.",
        "conflict": "The data already exists or conflicts with other data.",
        "authentication_failed": "We could not verify your identity.",
        "forbidden": "You are not allowed to do this.",
        "rate_limited": "You are making too many requests in a row. Please wait a moment.",
        "service_unavailable": "The service is not available right now. Please try again in a few seconds.",
        "internal_error": "Something went wrong. Please try again in a few seconds.",
        "payload_too_large": "The submitted file or message is too large.",
        "privacy_violation": "The request contains personal data that cannot be processed.",
        "route_not_found": "We could not find route {route_id}.",
        "stop_not_found": "We could not find stop {stop_id}.",
        "stop_not_in_route": "That stop is not on route {route_id} in that direction.",
        "bus_not_found": "We could not find bus {bus_id}.",
        "invalid_bus_id": "The bus identifier is not valid.",
        "bus_not_on_route": "Bus {bus_id} is not serving route {route_id}.",
        "too_many_routes": "You can query between 1 and {max_routes} routes at a time.",
        "route_invalid_stops": "The stops of route {route_id} do not follow the path.",
        "invalid_credentials": "The email or the password do not match.",
        "account_locked": "For your security, wait {minutes} minutes before trying again.",
        "session_expired": "Your session ended. Please sign in again.",
        "email_taken": "That email is already registered. Would you like to sign in?",
        "device_not_registered": "This phone is not registered. Open the app again to register it.",
        "password_policy": "Please check your password.",
        "password_too_short": "The password needs at least {min_length} characters.",
        "password_too_long": "The password cannot be longer than {max_length} characters.",
        "password_no_digit": "It looks like a number is missing, please check it.",
        "password_no_uppercase": "Add at least one uppercase letter.",
        "alert_limit_reached": "You already have the maximum number of active alerts. Cancel one to create another.",
        "alert_not_found": "We could not find that active alert.",
        "alert_not_deliverable": "We could not deliver the alert through any channel.",
        "audit_bus_silent": "Bus {bus_id} is not reporting right now.",
        "audit_no_data": "There is no data for bus {bus_id} near that time.",
        "audit_invalid_range": "The end date cannot be earlier than the start date.",
        "ai_disabled": "AI analysis is not enabled.",
        "ai_invalid_request": "The analysis request is not valid.",
        "occupancy.state.available_seats": "Seats available",
        "occupancy.state.standing_only": "Standing only",
        "occupancy.state.full": "Full",
        "occupancy.state.overcrowded": "Overcrowded",
        "occupancy.state.no_data": "Data not available",
        "alert.proximity.distance.one": "1 stop away",
        "alert.proximity.distance.many": "{stops} stops away",
        "alert.proximity.title": "Your {route_id} is {distance}",
        "alert.proximity.body": "Bus {bus_id} towards {stop_name}: {state_label} ({occupancy_pct}%).",
        "alert.proximity.next_bus": " The next one: {state_label}, {stops} stops away.",
        "alert.proximity.a11y": (
            "Alert: your bus {route_id}, number {bus_id}, is {distance}. "
            "Status: {state_label}, {occupancy_pct} percent occupancy."
        ),
        "alert.occupancy_warning.title": "Your bus {route_id} is crowded",
        "alert.occupancy_warning.body": "Bus {bus_id} is now «{state_label}» ({occupancy_pct}%).",
        "alert.occupancy_warning.alternative": " The next one ({next_bus_id}) has free seats.",
        "alert.occupancy_warning.a11y": (
            "Notice: bus {route_id} number {bus_id} is crowded. Status: {state_label}, "
            "{occupancy_pct} percent occupancy."
        ),
        "alert.bus_lost.title": "We lost your bus signal",
        "alert.bus_lost.body": "We have not received data from bus {bus_id} for over {seconds} seconds. Your alert is still active.",
        "alert.bus_lost.a11y": "Notice: we lost the signal of bus {bus_id}. Your alert is still active.",
        "alert.expired.title": "Your alert expired",
        "alert.expired.body": "{minutes} minutes passed and your bus {route_id} did not reach {stop_name}. You can create another alert.",
        "alert.expired.a11y": "Your alert for route {route_id} expired. You can create another one.",
        "ai_unavailable": "AI analysis is not available right now.",
        "finding.count_mismatch": "Bus {bus_id}: cameras and doors differ by more than {threshold} people for over {seconds} s.",
        "finding.low_confidence": "Bus {bus_id}: the count confidence dropped below {min_confidence}.",
        "finding.silent_bus": "Bus {bus_id} on route {route_id} has not reported for over {seconds} seconds.",
        "finding.digest.subject": "[Bus capacity] Daily findings digest ({count})",
        "finding.alert.subject": "[Bus capacity] {description}",
        "report.audit.subject": "[Bus capacity] Weekly accuracy report ({start} to {end})",
        "report.audit.summary": "Samples: {samples} | Mean error: {error}%",
        "report.audit.recalibrate": "Buses to recalibrate (error > {threshold}% in two consecutive audits): {buses}",
        "report.audit.none": "none",
        "report.audit.by_bus": "By bus:",
        "report.audit.by_model": "By model version:",
        "report.audit.line": "  {label}: {samples} samples, mean error {abs_error} people ({pct_error}%)",
    },
}


class _SafeDict(dict[str, Any]):
    """Leaves unknown placeholders untouched instead of raising ``KeyError``."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class MessageCatalog:
    """Looks up and renders localised messages with graceful fallbacks."""

    def __init__(self, messages: Mapping[str, Mapping[str, str]] | None = None) -> None:
        self._messages: Mapping[str, Mapping[str, str]] = messages if messages is not None else _MESSAGES

    @property
    def supported_locales(self) -> tuple[str, ...]:
        return tuple(self._messages)

    def normalize_locale(self, header: str | None) -> str:
        """Pick a supported locale from an ``Accept-Language`` style header."""
        if not header:
            return DEFAULT_LOCALE
        for part in header.split(","):
            language = part.split(";")[0].strip().lower().split("-")[0]
            if language in self._messages:
                return language
        return DEFAULT_LOCALE

    def has(self, key: str, locale: str | None = None) -> bool:
        return key in self._messages.get(locale or DEFAULT_LOCALE, {})

    def render(self, key: str, locale: str | None = None, **params: Any) -> str:
        """Render ``key`` in ``locale``; falls back to the default locale and then to the key."""
        chosen = locale if locale is not None and locale in self._messages else DEFAULT_LOCALE
        template = self._messages[chosen].get(key) or self._messages[DEFAULT_LOCALE].get(key)
        if template is None:
            return key
        return template.format_map(_SafeDict(params))
