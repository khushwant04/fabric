# Inference data-plane image.
#
# Build from the repository root:
#   docker build -f deploy/images/data-plane.Dockerfile -t fabric/data-plane:dev .

FROM python:3.12-slim-bookworm AS build

WORKDIR /src
COPY data-plane/pyproject.toml ./
COPY data-plane/fabric_data_plane ./fabric_data_plane

# A virtualenv is copied wholesale into the runtime stage, so build tooling and
# caches never reach the shipped image.
# Installed with pip rather than uv so the build needs only the Python base image.
# Versions are pinned in pyproject.toml, so the resolver choice does not change what
# lands in the image.
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip==24.3.1 && \
    /opt/venv/bin/pip install --no-cache-dir .

FROM python:3.12-slim-bookworm

# A dedicated unprivileged user: the data plane holds no credential and needs no
# write access to anything except its own temporary files.
RUN groupadd --gid 65532 nonroot && \
    useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin nonroot

# Only the virtualenv is copied. Copying the source as well would leave two copies
# of the package in the image, and which one imports would depend on the working
# directory.
COPY --from=build /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER 65532:65532

# 8080 serves inference. 8081 is administrative and is deliberately not published
# by the Service: only a collector sharing the pod's network namespace reaches it.
EXPOSE 8080 8081

ENTRYPOINT ["python", "-m", "uvicorn"]
CMD ["fabric_data_plane.main:inference", "--host", "0.0.0.0", "--port", "8080"]
