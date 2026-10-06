# Build from the repository root. The Dockerfile-specific ignore file excludes
# environment files and credentials before the context is uploaded.
FROM node:22-alpine AS dependencies
WORKDIR /app
COPY v1/package.json v1/package-lock.json ./
RUN npm ci

FROM dependencies AS build
ENV NEXT_TELEMETRY_DISABLED=1
COPY v1/ ./
RUN npm run build

FROM node:22-alpine AS runtime
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000
WORKDIR /app
RUN addgroup --system --gid 65532 fabric && adduser --system --uid 65532 --ingroup fabric fabric
COPY --from=build --chown=65532:65532 /app/.next/standalone ./
COPY --from=build --chown=65532:65532 /app/.next/static ./.next/static
COPY --from=build --chown=65532:65532 /app/public ./public
USER 65532:65532
EXPOSE 3000
CMD ["node", "server.js"]
