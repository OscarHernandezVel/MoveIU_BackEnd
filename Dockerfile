FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Push real con Firebase (opcional): docker build --build-arg INSTALL_PUSH=false para omitirlo.
ARG INSTALL_PUSH=true
RUN if [ "$INSTALL_PUSH" = "true" ]; then pip install --no-cache-dir "firebase-admin>=6.5"; fi
COPY src ./src
# demo_data: CORE_SEED_DEMO y la CLI de ingesta (python -m core_service.ingestion) leen estos archivos.
COPY demo_data ./demo_data
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh
ENV PYTHONPATH=/app/src CORE_HOST=0.0.0.0
# El servicio no corre como root.
RUN useradd --system --uid 10001 core
USER core
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/v1/health/live',timeout=2).status==200 else 1)"
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "core_service"]
