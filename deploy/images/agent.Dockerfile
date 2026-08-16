# Agent, collector, and operator image.
#
# All three ship in one image because they are versioned together and share a module,
# but they run as separate containers with separate mounts, separate credentials, and
# in the operator's case a separate ServiceAccount. Sharing an image is not sharing a
# trust boundary.
#
# Build from the repository root:
#   docker build -f deploy/images/agent.Dockerfile -t fabric/agent:dev .

FROM golang:1.22-bookworm AS build

WORKDIR /src

# Only the module files first, so dependency resolution is cached separately from
# source edits. The agent has no third-party dependencies today, which keeps this
# layer trivial and the supply chain small.
COPY agent/go.mod ./
RUN go mod download

COPY agent/ ./

# CGO disabled so the binaries are static and can run on a distroless base.
# Symbols and DWARF stripped: they are not useful in a container that ships no
# debugger, and they roughly halve the binary.
ARG VERSION=dev
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags "-s -w" \
        -o /out/fabric-agent ./cmd/fabric-agent && \
    CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags "-s -w" \
        -o /out/fabric-collector ./cmd/fabric-collector && \
    CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags "-s -w" \
        -o /out/fabric-operator ./cmd/fabric-operator

# Verify the binaries at least start before shipping them.
RUN /out/fabric-agent --version && /out/fabric-collector --version && \
    /out/fabric-operator --version

# Static distroless: no shell, no package manager, no libc to patch. Pinned by
# digest so a rebuild cannot silently change the base.
FROM gcr.io/distroless/static-debian12:nonroot@sha256:3d0f463de06b7ddff27684ec3bfd0b54a425149d0f8685308b1fdf297b0265e9

COPY --from=build /out/fabric-agent /usr/local/bin/fabric-agent
COPY --from=build /out/fabric-collector /usr/local/bin/fabric-collector
COPY --from=build /out/fabric-operator /usr/local/bin/fabric-operator

# 65532 is distroless' nonroot user. Declared explicitly so the manifests and the
# image agree on the UID that owns the state volume.
USER 65532:65532

ENTRYPOINT ["/usr/local/bin/fabric-agent"]
