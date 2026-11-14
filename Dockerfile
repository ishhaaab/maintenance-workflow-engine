FROM python:3.12-slim-bookworm@sha256:6dd8b6dab6b3adc7812bab69eca5dbdc1a5cc690ad28306d234ffb4ef8ac9753 AS dependencies
WORKDIR /app
RUN pip install --no-cache-dir uv==0.13.6 && groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --create-home app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev
COPY fixtures ./fixtures
COPY prompts ./prompts
COPY alembic.ini ./
COPY alembic ./alembic
COPY scripts/init_postgres_roles.py scripts/prepare_postgres.py ./scripts/
RUN mkdir /app/data && chown -R app:app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

FROM dependencies AS test-tools
RUN uv sync --frozen --extra dev
COPY tests ./tests
USER app
ENTRYPOINT ["python", "-m", "tests.compose_fixture"]

FROM dependencies AS production
USER app
ENTRYPOINT ["workflow-engine"]
CMD ["api", "--host", "0.0.0.0"]
