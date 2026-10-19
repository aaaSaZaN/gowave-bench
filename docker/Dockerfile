ARG GO_IMAGE=docker.io/library/golang:1.27.1
ARG RUNTIME_IMAGE=docker.io/library/debian:trixie-slim
FROM ${GO_IMAGE} AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY cmd/ ./cmd/
COPY config/ ./config/
COPY internal/ ./internal/
ARG REVISION=unknown
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-X gowave/internal/system.AppVersion=bench-${REVISION}" -o /panel ./cmd/server

FROM ${RUNTIME_IMAGE}
ARG REVISION=unknown
LABEL org.opencontainers.image.revision=${REVISION}
WORKDIR /app
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /panel /usr/local/bin/panel
USER 10001:10001
EXPOSE 3000
ENTRYPOINT ["/usr/local/bin/panel"]
