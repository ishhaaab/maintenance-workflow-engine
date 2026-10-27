FROM python:3.12-slim-bookworm@sha256:6dd8b6dab6b3adc7812bab69eca5dbdc1a5cc690ad28306d234ffb4ef8ac9753
WORKDIR /app
RUN pip install --no-cache-dir uv==0.13.6 && useradd --uid 10001 --create-home app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev
COPY fixtures ./fixtures
COPY prompts ./prompts
RUN mkdir /app/data && chown -R app:app /app
USER app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
ENTRYPOINT ["workflow-engine"]
CMD ["api", "--host", "0.0.0.0"]
