#!/bin/sh
# Punto de entrada del contenedor.
#
# En AWS ECS los secretos de Secrets Manager llegan como variables de entorno, pero Firebase necesita
# un ARCHIVO con la cuenta de servicio. Si se da CORE_FIREBASE_CREDENTIALS_JSON (el JSON completo),
# se escribe en un archivo privado y se apunta CORE_FIREBASE_CREDENTIALS a él.
set -eu

if [ -n "${CORE_FIREBASE_CREDENTIALS_JSON:-}" ] && [ -z "${CORE_FIREBASE_CREDENTIALS:-}" ]; then
    umask 077
    target="${TMPDIR:-/tmp}/firebase-credentials.json"
    printf '%s' "$CORE_FIREBASE_CREDENTIALS_JSON" > "$target"
    export CORE_FIREBASE_CREDENTIALS="$target"
    unset CORE_FIREBASE_CREDENTIALS_JSON
fi

exec "$@"
