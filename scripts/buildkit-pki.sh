#!/usr/bin/env bash
# Issues buildkitd's mTLS certificates and writes them as SOPS-encrypted
# Secrets: the server's for buildkitd, the client's for the tracon node.
#
# The CA key exists only for the length of this run. Nothing can mint another
# certificate afterwards, so rotating (or replacing a leaked key) means running
# this again: it reissues the CA and both leaves together, and buildkitd's
# liveness probe restarts it once Flux applies the new Secret.
#
# Usage: scripts/buildkit-pki.sh
set -euo pipefail

DAYS=1825
SERVER_SECRET=kubernetes/apps/base/tracon-build/buildkitd-certs-secret.yaml
CLIENT_SECRET=kubernetes/apps/base/tracon-node/buildkit-client-certs-secret.yaml

repo=$(git rev-parse --show-toplevel)
for tool in openssl sops; do
  command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done

# Keys and plaintext Secrets stay in a private temp dir; only ciphertext is
# written into the repository.
umask 077
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cd "$work"

new_key() { openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$1" 2>/dev/null; }

new_key ca.key
openssl req -x509 -new -key ca.key -out ca.crt -days "$((DAYS + 1))" \
  -subj "/CN=tracon-build buildkit CA" \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
  -addext "keyUsage=critical,keyCertSign,cRLSign"

# Usages are explicit, so the server certificate cannot act as a client and
# the client certificate cannot act as the server.
leaf() { # name, CN, extendedKeyUsage, openssl verify purpose, [extra extension]
  new_key "$1.key"
  openssl req -new -key "$1.key" -subj "/CN=$2" -out "$1.csr"
  printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=%s\n%s\n' \
    "$3" "${5:-}" >"$1.ext"
  openssl x509 -req -in "$1.csr" -CA ca.crt -CAkey ca.key -days "$DAYS" \
    -set_serial "0x$(openssl rand -hex 16)" -extfile "$1.ext" -out "$1.crt" 2>/dev/null
  openssl verify -CAfile ca.crt -purpose "$4" "$1.crt" >/dev/null
}
leaf server buildkitd.tracon-build.svc serverAuth sslserver \
  "subjectAltName=DNS:buildkitd.tracon-build.svc,DNS:buildkitd.tracon-build.svc.cluster.local"
leaf client tracon-node clientAuth sslclient
rm -f ca.key

expires=$(date -u -d "$(openssl x509 -enddate -noout -in server.crt | cut -d= -f2)" +%F)

secret() { # name, namespace, leaf, repository path
  {
    printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: %s\n  namespace: %s\n' "$1" "$2"
    printf '  annotations:\n    homelab/expires: "%s"\n    homelab/issued-by: scripts/buildkit-pki.sh\n' "$expires"
    printf 'type: Opaque\nstringData:\n'
    printf '  ca.crt: |\n';  sed 's/^/    /' ca.crt
    printf '  tls.crt: |\n'; sed 's/^/    /' "$3.crt"
    printf '  tls.key: |\n'; sed 's/^/    /' "$3.key"
  } >"$3.yaml"
  sops --config "$repo/.sops.yaml" encrypt --filename-override "$repo/$4" "$3.yaml" >"$3.enc.yaml"
  install -m 0644 "$3.enc.yaml" "$repo/$4"
}
secret buildkitd-certs tracon-build server "$SERVER_SECRET"
secret buildkit-client-certs tracon-node client "$CLIENT_SECRET"

echo "wrote $SERVER_SECRET and $CLIENT_SECRET (expire $expires)"
