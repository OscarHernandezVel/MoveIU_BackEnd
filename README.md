# core-service — Servicio central del sistema de capacidad neta de buses

Servicio central (*core service*) del **Sistema de detección de capacidad neta de buses en tiempo real**.
Recibe la telemetría del hardware a bordo (cámaras + sensores de puertas + GPS), calcula la ocupación
neta de cada bus, la publica para los pasajeros, dispara alertas en pantalla y prepara los datos y las
herramientas para que una IA analice la flota.

> **Convención del proyecto.** El código (identificadores, módulos y nombres de archivos) está en **inglés**;
> toda la documentación está en **español**. Los textos que ve el pasajero viven en un catálogo de mensajes
> (`src/core_service/i18n/catalog.py`, español por defecto e inglés como segunda opción).

Solo usa **Python** (≥ 3.10) y programación orientada a objetos. Las soluciones algorítmicas se basan en
**pilas LIFO, arreglos y listas doblemente enlazadas**, implementadas a mano en `data_structures/`.

## Inicio rápido

```bash
python -m venv .venv && source .venv/bin/activate      # en Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

docker compose -f docker-compose.dev.yml up -d           # PostgreSQL (TimescaleDB + PostGIS) + Redis
python scripts/init_dev_db.py                            # crea las tablas del documento v7
export CORE_TEST_DATABASE_URL=postgresql://core:core@localhost:5432/core_test
pytest                                   # unitarias y de punta a punta contra PostgreSQL real
ruff check src tests && mypy src         # estilo y tipado estricto

# Levantar el servicio con datos de demostración (ruta C5 y tres buses)
export CORE_SEED_DEMO=true CORE_INGEST_KEY=clave-de-desarrollo
export CORE_DATABASE_URL=postgresql://core:core@localhost:5432/core CORE_REDIS_URL=redis://:core-dev-redis@localhost:6379/0
python -m core_service                   # http://127.0.0.1:8080/docs

# En otra terminal: un bus simulado recorriendo la ruta
python simulator/simulate_bus.py --key clave-de-desarrollo --bus 1045 --loops 2
```

La configuración completa se hace con variables `CORE_*` (ver `.env.example`).
En `CORE_ENV=production` el arranque **falla** si falta un secreto o la configuración es insegura.

## Estructura de carpetas

```text
backend/                          (el front-end está en ../frontend y el hardware a bordo en ../edge)
├── README.md                     ← este documento
│   ├── architecture.md           orquestación de servicios, dependencias y bases de datos
│   ├── algorithms.md             contenido algorítmico paso a paso
│   ├── api.md                    contrato HTTP
│   ├── quality-and-security.md   seguridad, fiabilidad, usabilidad y mantenibilidad
│   ├── ai-integration.md         cómo queda el sistema preparado para una IA
│   ├── data-layer.md             PostgreSQL, TimescaleDB, Redis, seguridad y alta disponibilidad
│   ├── realtime.md               WebSocket del mapa: protocolo, algoritmo, réplicas y límites
│   ├── docker-desktop.md         crear la base de datos en Docker Desktop paso a paso
│   ├── data-model-v7.md          tablas del documento v7 ↔ código, PostGIS y desviaciones
│   ├── ingestion.md              ingesta de rutas, paradas y buses (paquete, CLI, recarga)
│   ├── opus-api.md               requisitos de la API de Opus 5.5 y límites de un modelo cerrado
│   └── audit-report.md           revisión de lógica de negocio y de punteros de las listas
├── demo_data/route_c5.json       ruta, paradas y buses de demostración (sintética)
├── demo_data/pasto_c1_c4.json    rutas C1–C4 reales de Pasto + 8 buses ficticios
├── demo_data/fleet_pasto.json    flota ficticia de las rutas C1–C4
├── simulator/simulate_bus.py     simula el hardware de un bus (solo biblioteca estándar)
├── simulator/simulate_fleet.py   simula todos los buses activos de un paquete a la vez
├── simulator/watch_stream.py     sigue rutas por el WebSocket como lo haría la app
├── ../edge/                      a bordo de la maqueta: Arduino (puertas), Raspberry Pi (cámaras, GPS,
│                                 cola SQLite, envío), diagrama de conexión y planos 1:20 (ver edge/README.md)
├── pyproject.toml                pytest, ruff y mypy
├── docker-compose.dev.yml        TimescaleDB + Redis para desarrollo y pruebas
├── Dockerfile                    imagen del servicio (usuario sin privilegios)
├── deploy/                       roles de PostgreSQL, ACL/TLS de Redis y referencia de alta disponibilidad
├── scripts/verify_data_layer.py  comprueba permisos reales de Redis y PostgreSQL
├── src/core_service/
│   ├── data_structures/          Stack · DoublyLinkedList · Array · CircularArray · Node
│   ├── models/                   entidades y objetos de valor (Bus, Reading, LiveBusState…)
│   ├── occupancy/                fórmula, mapa de asientos, fusión cámaras/puertas, historial
│   ├── routing/                  geometría de la ruta, paradas, dirección de viaje
│   ├── comparator/               «este bus o el siguiente»
│   ├── alerts/                   motor de proximidad y alertas en pantalla
│   ├── auth/                     invitado, cuenta opcional, JWT, bloqueo por intentos
│   ├── audit/                    auditorías manuales, métricas de error, informe semanal
│   ├── ai/                       contexto privado, herramientas, contrato JSON, respaldo por reglas
│   ├── realtime/                 WebSocket: protocolo, diferencias por ruta, hub con contrapresión
│   ├── ingestion/                paquete core-catalog/1, adaptador de Avante, cargador y CLI
│   ├── persistence/
│   │   ├── relational/           PostgreSQL + TimescaleDB: usuarios, catálogo, alertas, auditorías, telemetría
│   │   └── documents/            Redis (TTL, Lua, conjuntos); en memoria/registro solo para pruebas o desarrollo
│   ├── services/                 servicios de aplicación, bus de eventos, Saga, trabajos periódicos
│   ├── security/                 limitador de tasa de ventana deslizante
│   ├── notifications/            correo al equipo técnico
│   ├── api/                      FastAPI: routers, esquemas, errores, middleware
│   ├── container.py              raíz de composición (crea y conecta todos los servicios)
│   ├── config.py · clock.py · errors.py · metrics.py · logging_config.py
│   └── __main__.py               `python -m core_service`
└── tests/
    ├── unit/                     pruebas unitarias por módulo
    ├── e2e/                      pruebas de punta a punta sobre HTTP
    └── factories.py · fakes.py · harness.py · system_harness.py
```

## Contenido algorítmico

Cada estructura de datos resuelve un problema concreto del dominio.

| Estructura | Dónde se usa | Qué resuelve | Costo |
|---|---|---|---|
| **Pila LIFO** (`Stack`, sobre lista doble) | `occupancy/reading_history.py` | La cima es el estado vivo del bus; la fusión cámaras/puertas recorre solo los últimos *k* reportes; el terminal vacía la pila (`drain`) | `push/pop/peek` O(1), recorrido O(k) |
| Pila LIFO | `services/saga.py` | Deshacer en orden inverso una operación de varios pasos (historial → estado vivo → SQL) | O(pasos) |
| Pila LIFO | `alerts/screen_inbox.py` | Bandeja de alertas en pantalla: lo más reciente primero, acotada por dispositivo | `push` O(1) |
| Pila LIFO | `auth/login_attempts.py` | Marcas de tiempo de intentos fallidos: 3 fallos en 5 minutos bloquean la cuenta | O(1) amortizado |
| **Lista doblemente enlazada** (`DoublyLinkedList`) | `routing/route_track.py` | Paradas de la ruta: una sola lista sirve a los dos sentidos (ida = `next`, vuelta = `prev`) | vecino O(1) |
| Lista doble + **arreglos tipados** + **rejilla espacial** | `routing/geometry.py` | Vértices de la ruta; proyección del GPS exacta con índice de celdas y continuidad entre tramos de una misma calle | proyección O(1) media (~27 µs en C1, antes ~710 µs) |
| Lista doble + `dict` de nodos | `alerts/alert_index.py` | Alertas activas por parada; cancelar/expirar es O(1) por el puntero al nodo | alta/baja O(1) |
| Lista doble (LRU) | `security/rate_limiter.py` | Limita cuántos clientes se recuerdan sin perder O(1) | O(1) |
| **Arreglo dinámico** (`Array`) | `routing/route_track.py` | Progreso ordenado de cada parada → **búsqueda binaria** de la primera parada por delante del bus | O(log n) |
| Arreglo | `occupancy/seat_map.py` | Una celda por posición del interior (asiento, de pie, silla de ruedas) | pasada única O(n) |
| Arreglo + **merge sort estable** | `audit/`, `metrics.py`, lotes de lecturas | Ordenar hallazgos por fecha, percentiles de latencia, ráfagas sin conexión | O(n log n) |
| **Montículo binario** (`BinaryHeap`, sobre `Array`) | `comparator/`, `ai/context_builder.py` | Los k buses más cercanos o más llenos sin ordenar todos | O(n log k) |
| **Arreglo circular** (`CircularArray`) | `alerts/throttle.py`, `security/rate_limiter.py`, `ai/circuit_breaker.py`, `metrics.py` | Ventanas deslizantes de tamaño fijo (límite de alertas, de peticiones, resultados de la IA, latencias) | O(1) y memoria fija |

Otros algoritmos del dominio: fórmula de **ocupación real** con posiciones de pie entre asientos,
**fusión cámaras/puertas** por ventana de desacuerdo, **detección de dirección** robusta (mediana repetida de Siegel),
**diferencias por ruta** para el WebSocket, **estadísticas en línea** para la IA (medias exponenciales en el tiempo),
comparador de **este bus o el siguiente**, **proximidad por paradas** con aciertos consecutivos,
tubería **deduplicar → admitir → entregar** con reintentos, y el **cortacircuitos** de la IA.

## Fórmula de ocupación

```text
ocupación % = (asientos ocupados + personas de pie) / (asientos + posiciones de pie + sillas de ruedas) × 100
```

Solo hay una persona de pie entre dos asientos; las filas de puertas y la fila trasera de cinco asientos no
suman posiciones de pie. Los niños en brazos cuentan como personas a bordo pero no ocupan capacidad.
Estados: `available_seats`, `standing_only`, `full`, `overcrowded` y `no_data` (siempre con icono y texto,
nunca solo color).

## Bases de datos: PostgreSQL + TimescaleDB + Redis

| Motor | Datos | Por qué |
|---|---|---|
| **PostgreSQL 16** (+ **PostGIS**) | usuarios, dispositivos, rutas y paradas por sentido, buses y su mapa interior, alertas (fuente de verdad), hallazgos, auditorías | Integridad referencial, `CHECK`, transacciones, PITR; geometría y `ST_LineLocatePoint` |
| **TimescaleDB** | historial de telemetría (*hypertable*), compresión, retención y agregado continuo de 5 minutos | Series temporales de alto volumen; tendencias para la IA en milisegundos |
| **Redis 7** | estado vivo, bandeja de alertas, sesiones, deduplicación, bloqueos, límites de peticiones, historial compartido | Latencia baja, TTL nativo, atomicidad (Lua), visible para todas las réplicas |

## Preparado para una IA

El backend expone a la IA solo **datos numéricos de la flota**, por **herramientas de solo lectura**, y solo
acepta de ella un **JSON estricto** con acciones de un conjunto cerrado. Si la IA no está disponible, un
analizador por reglas responde en su lugar. Con cada lectura se actualizan en O(1) **estadísticas en línea**
por bus (sesgo cámaras/puertas, puntuación z, confianza, intervalo de reporte) y se calcula la **separación
entre buses** de cada ruta; la IA los recibe ya agregados.

## Tiempo real

`WS /ws/v1/buses/stream` (v7): la app se suscribe a sus rutas y recibe cada 3 s solo los buses que cambiaron.
Una lectura por lotes de Redis por tick, una serialización por ruta compartida por todos los celulares y colas
acotadas por conexión.

## Calidad de software

* **Seguridad**: PBKDF2, JWT con algoritmo fijo, tokens de refresco de un solo uso, bloqueo por intentos,
  límite de tasa, validación estricta de entradas, SQL parametrizado, cabeceras seguras, secretos exigidos
  en producción, sin datos personales hacia la IA ni en los registros.
* **Fiabilidad**: transacciones SQL, Saga con compensaciones LIFO, entrega a-lo-menos-una-vez con
  deduplicación, reintentos con espera exponencial, cortacircuitos, aislamiento de fallos entre
  suscriptores y trabajos, réplicas con estado compartido, conmutación por error y recuperación tras reinicio
  (reconstrucción de índices desde PostgreSQL/Redis).
* **Usabilidad**: mensajes en lenguaje sencillo y localizados, errores accionables, alertas con etiqueta de
  accesibilidad, vibración y sonido, estado nunca comunicado solo por color.
* **Mantenibilidad**: capas con dependencias hacia adentro, inyección de dependencias, reloj inyectable,
  tipado estricto (`mypy --strict`), `ruff`, migraciones versionadas y pruebas de cada módulo.


## Pruebas

```bash
pytest tests/unit                         # unitarias (estructuras, fórmula, servicios, IA…)
pytest tests/e2e                          # punta a punta: HTTP real, SQL real, solo el mundo exterior simulado
pytest --cov=core_service --cov-report=term-missing
```

En las pruebas se sustituyen únicamente el reloj (`FakeClock`), el proveedor de notificaciones, el correo y
el modelo de lenguaje; todo lo demás es el código real.

## Limitaciones conocidas

* El adaptador de notificaciones Firebase (`alerts/firebase_gateway.py`) y el envío SMTP real no se
  probaron contra los servicios reales (las pruebas usan dobles). El cliente de Anthropic se prueba con un
  transporte simulado, no contra la API real.
* El WebSocket del mapa (`/ws/v1/buses/stream`) está construido y probado (también con uvicorn y Redis reales),
  pero no se han hecho pruebas de carga con miles de conexiones; MQTT hacia los celulares no está implementado.
* La ruta **real** de TimescaleDB, Redis con TLS/Sentinel y la conmutación por error no se probaron contra
  despliegues reales.
* La vuelta de cada ruta se guarda real en las tablas, pero el dominio la sirve como la ida recorrida al revés
  (la recarga del catálogo lo avisa). Los buses del paquete de demostración y los ROI de sus mapas son ficticios / sin calibrar.
  Los datos de rutas de Pasto vienen de RutiPasto, **cuyo repositorio no declara licencia**: confirme los términos.
* No se ha medido carga: las complejidades documentadas son teóricas.
