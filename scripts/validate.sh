#!/usr/bin/env bash
# Renders what Flux applies and checks it: every Secret is SOPS-encrypted,
# every object matches its schema, and workloads pass kube-linter. CI runs
# this on every pull request and on main.
#
# flux-system/ is skipped: `flux bootstrap` generates it.
#
# Needs kustomize, kubeconform, kube-linter and yq (mikefarah) on PATH.
# Usage: scripts/validate.sh
set -euo pipefail

cd "$(dirname "$0")/.."

# Patch releases share a schema set; the minor tracks the cluster.
K8S_VERSION=1.35.0
CRD_SCHEMAS='https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'

out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT

kustomize build kubernetes/infrastructure/controllers >"$out/infrastructure.yaml"
kustomize build kubernetes/apps/production >"$out/apps.yaml"
cp kubernetes/clusters/production/apps.yaml "$out/flux-apps.yaml"
cp kubernetes/clusters/production/infrastructure.yaml "$out/flux-infrastructure.yaml"

echo "--- Secrets are SOPS-encrypted"
plain=$(yq ea 'select(.kind == "Secret" and .sops == null) | .metadata.namespace + "/" + .metadata.name' "$out"/*.yaml)
if [ -n "$plain" ]; then
  echo "Secrets without SOPS encryption (run sops --encrypt --in-place):" >&2
  echo "$plain" >&2
  exit 1
fi
echo "ok"

# The sops block is SOPS's own metadata, not part of the Secret, so strip it
# before strict validation (which rejects unknown fields).
echo "--- Schemas"
yq ea 'del(.sops)' "$out"/*.yaml |
  kubeconform -strict -summary \
    -kubernetes-version "$K8S_VERSION" \
    -schema-location default \
    -schema-location "$CRD_SCHEMAS"

echo "--- kube-linter"
kube-linter lint --config .kube-linter.yaml "$out"
