# homelab

GitOps-managed Kubernetes cluster running on DigitalOcean. Flux watches this repo's `main` branch and reconciles the cluster state continuously.

> Personal homelab — published for reference, not maintained as a public project. No support, no warranty.

## Cluster overview

| Component | Detail |
|---|---|
| Provider | DigitalOcean Kubernetes |
| Storage class | `do-block-storage-retain` (block volumes survive PVC deletion — manual cleanup required) |
| Ingress | Cloudflare Tunnel (`cloudflared`, outbound only; no load balancer) for HTTP. The one exception is the Minecraft NodePort (30565), admitted by its own cloud firewall |
| Auth | Cloudflare Access on `rss` and `tracon-node` |
| Network | A NetworkPolicy on every app pod (only tracon-node keeps open egress, for its proxies), plus a CiliumNetworkPolicy where a pod needs the API server; Pod Security Admission labels on each app namespace |
| TLS | Public: Cloudflare edge. Internal: buildkit mTLS from `scripts/buildkit-pki.sh` |
| GitOps | Flux v2 |
| Secrets | SOPS + age |
| Domain | `0x69.xyz` |

## Applications

| App | URL | How deployed |
|---|---|---|
| cloudflared | n/a | Raw Deployment + ConfigMap (hostname routes) |
| Immich | `immich.0x69.xyz` | Helm chart (immich-charts), custom Postgres sidecar |
| FreshRSS | `rss.0x69.xyz` | Raw Deployment + PVC |
| Minecraft (Hearth & Hollow) | `mc.0x69.xyz` (game), `mc-wake.0x69.xyz` | Raw StatefulSet + PVC on a 0-1 node pool, mc-router, wake page |
| external-dns | n/a | Helm chart (kubernetes-sigs), Cloudflare provider |

### cloudflared
The cluster's only way in. Two replicas hold outbound connections to Cloudflare for the `homelab` tunnel, and `kubernetes/apps/production/cloudflared/config.yaml` maps each public hostname to its Service (generated into a hash-suffixed ConfigMap, so a change rolls the pods). The tunnel credentials are the SOPS secret beside the Deployment. Access applications and service tokens live in the Zero Trust dashboard, not in this repo.

### Immich
Reachable without Access: the mobile app can't complete a browser login, so Immich's own login is the gate. Its NetworkPolicies confine the server to its database, its queue and the internet. Uses the official Helm chart with a self-managed Postgres deployment (`ghcr.io/immich-app/postgres`) that includes the Postgres extensions Immich requires. Machine learning is disabled. Library data is on a 100Gi PVC. The Postgres image version should be kept in sync with the Immich app version — check the official Immich docker-compose release for the matching tag.

### FreshRSS
Simple raw Deployment. Feeds refresh every 30 minutes via internal cron.

### Minecraft (Hearth & Hollow)
A whitelisted server for the [Hearth & Hollow](https://github.com/cosmicspork/hearth-and-hollow) modpack (`itzg/minecraft-server` with the release's server pack), scaled to zero when nobody plays:

- **mc-router** listens on the `minecraft` NodePort. While the server sleeps it answers the server list itself. A login scales the `hearth` StatefulSet to 1 and kicks the player with a "waking up" message, and an hour with nobody connected scales it back to 0. It owns the replica count, so the StatefulSet has no `replicas` for Flux to reset.
- **The server** runs alone on the `minecraft` node pool (`s-4vcpu-8gb-amd`, autoscaled 0-1, label and taint `workload=minecraft`), so that node exists only while someone plays. From asleep, a join takes roughly the new node's boot, the volume attach, the image pull and about 30 s of server start.
- **The wake page** (`mc-wake.0x69.xyz`) shows whether the server is up and wakes it, so it boots while players launch the game. It does that by logging in to mc-router the way a client would, so it needs no API access. It is open without Access on purpose: all it can do is start the server, which joining does anyway, and anyone waking it without playing only costs an hour of node time.
- **DNS**: external-dns publishes `mc.0x69.xyz` (the node mc-router runs on) and the `_minecraft._tcp` SRV record for the NodePort as DNS-only Cloudflare records, and republishes them when an upgrade replaces the node.
- **Updates**: Renovate tracks the pack's GitHub releases through `PACK_VERSION` and never automerges them (or the itzg images): a pack release is merged once CurseForge has approved the matching client.

Outside this repo: the `minecraft` node pool, the `homelab-minecraft` cloud firewall (TCP 30565 inbound on the cluster's worker tag; the DOKS-managed firewall admits nothing from outside), the Cloudflare API token in `external-dns/secret.yaml` (Zone:DNS:Edit and Zone:Zone:Read on `0x69.xyz`), and the proxied `mc-wake` tunnel CNAME.

The world lives on the `hearth-data` volume. The pod's `world-guard` init container waits until `/data/world/level.dat` exists, so an empty volume never generates a new world: to import one, copy it in with `kubectl cp <world-dir> minecraft/hearth-0:/data/world -c world-guard` while the guard is waiting. Simple Backups writes zips to `/data/simplebackups` on the same volume, so take a volume snapshot now and then.

Server console commands (game rules, Chunky, `whitelist add`) go through `kubectl -n minecraft exec hearth-0 -c server -- mc-send-to-console <command>` while it is awake; output lands in the pod log.

## Repository structure

```
kubernetes/
  clusters/production/       # Flux entrypoint — bootstrapped by `flux bootstrap`
    flux-system/             # Auto-generated by Flux (do not edit)
    infrastructure.yaml      # Flux Kustomization for infrastructure/controllers
    apps.yaml                # Flux Kustomization for apps/production
  infrastructure/
    controllers/             # Cluster-wide operators and controllers
      metrics-server/        # Resource Metrics API (kubectl top); HelmRelease lives in kube-system
  apps/
    base/                    # Environment-agnostic base manifests
      cloudflared/
      immich/
      freshrss/
      minecraft/             # Hearth & Hollow server, mc-router and the wake page
      external-dns/          # Cloudflare DNS records for NodePort Services
    production/              # Production overlays and environment-specific resources
      cloudflared/           # Tunnel config (config.yaml): public hostname -> Service
      immich/
      freshrss/
      minecraft/
      external-dns/
scripts/
  buildkit-pki.sh            # Issues tracon-build's mTLS certificates as SOPS secrets
  validate.sh                # Renders the manifests and runs the checks CI requires
.github/workflows/           # CI: manifest validation (required on main), scheduled Renovate
.sops.yaml                   # SOPS rules — which files to encrypt, with which age key
renovate.json                # Renovate config for dependency PRs
```

Flux reconciliation order: `infrastructure` → `apps` (enforced via `dependsOn`). Infrastructure has `wait: true` so all operators and CRDs are ready before apps are applied.

## Validation

`scripts/validate.sh` renders what Flux applies and fails if any Secret is not SOPS-encrypted, if an object doesn't match its schema (`kubeconform -strict`), or if a workload fails `kube-linter` (`.kube-linter.yaml`, which also requires every pod to be selected by a NetworkPolicy). The `validate` workflow runs it on every pull request and on `main`; the `main` ruleset requires it, and Renovate automerges minor and patch updates only once it passes. An object that can't meet a lint check carries an `ignore-check.kube-linter.io/<check>` annotation saying why. Run it locally with `kustomize`, `kubeconform`, `kube-linter` and `yq` on PATH. The workflow pins those tools by version and checksum, so they are bumped by hand.

Nothing in CI talks to the cluster: the DOKS control plane firewall admits only an allowlist. Flux fetches `main` every 5 minutes (a patch in `flux-system/kustomization.yaml`); `flux reconcile source git flux-system` from an allowed address applies a merge immediately.

## Secrets management (SOPS + age)

All `secret.yaml` files in this repo are encrypted with [SOPS](https://github.com/getsops/sops) using an [age](https://github.com/FiloSottile/age) key. Only the `stringData`/`data` fields are encrypted — the rest of the file is plain YAML.

### Key location
- **Private key**: `~/.config/sops/age/keys.txt` — back this up (password manager, etc.). It is not in this repo. Losing it means losing the ability to decrypt secrets.
- **Public key**: `age1m06mlc98kyfx0ewtrmjezlgca2zvfp0qtdc6apeum9w387mdqc2qkkd0a2` (in `.sops.yaml`)

### Working with secrets
```bash
# Decrypt and view
sops -d kubernetes/apps/production/<app>/secret.yaml

# Edit a secret (opens decrypted in $EDITOR, re-encrypts on save)
sops kubernetes/apps/production/<app>/secret.yaml

# Create a new secret — write plain YAML, then encrypt in place
sops --encrypt --in-place kubernetes/apps/some-app/secret.yaml
```

The `.sops.yaml` at the repo root configures SOPS to automatically use the age key for any file matching `kubernetes/.*secret\.yaml$`.

### buildkit mTLS certificates
`scripts/buildkit-pki.sh` issues a private CA, buildkitd's server certificate and the tracon node's client certificate, writes them as `buildkitd-certs-secret.yaml` (tracon-build) and `buildkit-client-certs-secret.yaml` (tracon-node), and discards the CA key, so nothing can mint another certificate. Each Secret's `homelab/expires` annotation records the expiry (five years). To rotate, or to replace a leaked key, run the script again and commit both files; buildkitd restarts on its own once Flux applies them.

### Flux decryption
The age private key is loaded into the cluster as a secret that Flux uses to decrypt at apply time:
```bash
kubectl create secret generic sops-age \
  --namespace=flux-system \
  --from-file=age.agekey=$HOME/.config/sops/age/keys.txt
```
This secret must exist before Flux can apply any app that has encrypted secrets.

## Cluster rebuild procedure

If the cluster needs to be rebuilt from scratch:

1. **Provision a new DOKS cluster** via DigitalOcean.

2. **Restore your age private key** to `~/.config/sops/age/keys.txt`.

3. **Create the SOPS secret** so Flux can decrypt secrets at apply time:
   ```bash
   kubectl create namespace flux-system
   kubectl create secret generic sops-age \
     --namespace=flux-system \
     --from-file=age.agekey=$HOME/.config/sops/age/keys.txt
   ```

4. **Bootstrap Flux** against this repo:
   ```bash
   flux bootstrap github \
     --owner=cosmicspork \
     --repository=homelab \
     --branch=main \
     --path=kubernetes/clusters/production
   ```
   Flux will apply everything in order: infrastructure → apps. The tunnel reconnects on its own, since its credentials are in the repo and DNS points at the tunnel, not the cluster.

   The rest of the public edge lives in the Cloudflare account, not this repo: the `homelab` tunnel itself, the proxied DNS records (`cloudflared tunnel route dns`), and the Zero Trust Access applications, policies and service tokens. A cluster rebuild leaves them untouched. If the tunnel was deleted, create a new one, replace `secret.yaml` and the tunnel ID in the cloudflared ConfigMap, and re-run `route dns --overwrite-dns` for each hostname.

5. **Check rollout**:
   ```bash
   flux get all -A
   ```

### Notes on persistent data
The storage class `do-block-storage-retain` means DigitalOcean block volumes are **not deleted** when their PVC is deleted. On a full cluster rebuild the volumes will still exist in your DO account. To reattach them, recreate the PVs and PVCs with the matching volume IDs before Flux applies. If starting fresh, delete the old volumes in the DO console first to avoid orphaned billing.

## Adding things

### A new application
1. Create `kubernetes/apps/base/<app>/` with namespace + base manifests and a `kustomization.yaml`.
2. Create `kubernetes/apps/production/<app>/` with a `kustomization.yaml` referencing the base, plus any production-specific overlays.
3. Add the app directory to `kubernetes/apps/production/kustomization.yaml`.
4. If the app needs secrets, write a `secret.yaml` and run `sops --encrypt --in-place` before committing.
5. Add a `networkpolicy.yaml`: a namespace-wide default deny, then what the app needs (see `freshrss/` for the shape). Label the namespace with the strictest Pod Security level the image meets.
6. If the app is public, add a hostname rule to the cloudflared ConfigMap, an egress rule to the app's pod in `cloudflared/networkpolicy.yaml`, run `cloudflared tunnel route dns homelab <host>.0x69.xyz`, and add an Access application in Zero Trust unless the app must be reachable without one.

### A new infrastructure controller
1. Create `kubernetes/infrastructure/controllers/<name>/` with namespace, HelmRepository, HelmRelease, and `kustomization.yaml`.
2. Add the directory to `kubernetes/infrastructure/controllers/kustomization.yaml`.

## Prerequisites (local tooling)

| Tool | Purpose |
|---|---|
| `kubectl` | Cluster access |
| `flux` | Flux CLI |
| `helm` | Chart inspection / manual operations |
| `sops` | Encrypt/decrypt secrets |
| `age` / `age-keygen` | Key generation (one-time setup) |
| `cloudflared` | Tunnel DNS routes (`tunnel route dns`); needs `~/.cloudflared/cert.pem` from `cloudflared tunnel login` |
