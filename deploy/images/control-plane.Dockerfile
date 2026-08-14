# Control-plane image.
#
# Build from the repository root:
#   docker build -f deploy/images/control-plane.Dockerfile -t fabric/control-plane:dev .

FROM python:3.12-slim-bookworm AS build

WORKDIR /src
COPY control-plane/pyproject.toml ./
COPY control-plane/app ./app

# Installed with pip rather than uv so the build needs only the Python base image.
# Versions are pinned in pyproject.toml, so the resolver choice does not change what
# lands in the image.
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip==24.3.1 && \
    /opt/venv/bin/pip install --no-cache-dir .

FROM python:3.12-slim-bookworm

RUN groupadd --gid 65532 nonroot && \
    useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin nonroot

# Only the virtualenv holds the application package, so there is exactly one copy
# of the code in the image.
COPY --from=build /opt/venv /opt/venv
# Migrations are not part of the package, and ship so a deployment can run its own
# schema upgrade rather than depending on a developer's checkout.
COPY control-plane/migrations /app/migrations
COPY control-plane/alembic.ini /app/alembic.ini

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
USER 65532:65532

EXPOSE 8080

ENTRYPOINT ["python", "-m", "uvicorn"]
CMD ["app.main:app", "--host", "0.0.0.0", "--port", "8080"]
