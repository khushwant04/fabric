# Code-interpreter sandbox image (docs/hosted-tools.md).
#
# Build from the repository root:
#   docker build -f deploy/images/sandbox.Dockerfile -t fabric/sandbox:dev .
#
# Two environments: /opt/service runs the HTTP service; /opt/kernel is the interpreter that
# executes model-written code and holds only the pinned scientific stack. User code is
# started with `python -I -S` and the kernel's site directories, so it does not import the
# service's packages by accident (it is not a security boundary: both are readable).

FROM python:3.12-slim-bookworm AS build

WORKDIR /src
COPY sandbox/pyproject.toml ./
COPY sandbox/fabric_sandbox ./fabric_sandbox

RUN python -m venv /opt/service && \
    /opt/service/bin/pip install --no-cache-dir --upgrade pip==24.3.1 && \
    /opt/service/bin/pip install --no-cache-dir . && \
    python -m venv /opt/kernel && \
    /opt/kernel/bin/pip install --no-cache-dir --upgrade pip==24.3.1 && \
    /opt/kernel/bin/pip install --no-cache-dir numpy==2.1.3 pandas==2.2.3 matplotlib==3.9.2 && \
    /opt/kernel/bin/pip uninstall --yes pip

FROM python:3.12-slim-bookworm

# Unprivileged, and a different uid from the gateway (65532): the sandbox never shares an
# identity with anything that holds a credential other than its own shared secret.
RUN groupadd --gid 10001 sandbox && \
    useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin sandbox

COPY --from=build /opt/service /opt/service
COPY --from=build /opt/kernel /opt/kernel

ENV PATH="/opt/service/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FABRIC_SANDBOX_PYTHON=/opt/kernel/bin/python \
    FABRIC_SANDBOX_ROOT_DIR=/var/lib/fabric-sandbox
USER 10001:10001

EXPOSE 8090
ENTRYPOINT ["python", "-m", "fabric_sandbox"]
