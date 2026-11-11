# homelab

GitOps-managed Kubernetes cluster running on DigitalOcean. Flux watches this repo's `main` branch and reconciles the cluster state continuously.

> Personal homelab — published for reference, not maintained as a public project. No support, no warranty.

## Cluster overview

| Component | Detail |
|---|---|
| Provider | DigitalOcean Kubernetes, `ams3` (moved from `nyc1` in November 2026, when nyc1 ran out of capacity to replace nodes) |
| Storage class | `do-block-storage-retain` (block volumes survive PVC deletion — manual cleanup required) |
| Backups | VolSync (restic) copies every stateful volume to Cloudflare R2 (see Backups) |
| Ingress | Cloudflare Tunnel (`cloudflared`, outbound only; no load balancer) for HTTP. The one exception is the Minecraft NodePort (30565), admitted by its own cloud firewall |
| Auth | Cloudflare Access on `rss`, `tracon-node`, `mc-wake`, `metrics` and `k8s` (the Kubernetes API) |
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
| Monitoring | `metrics.0x69.xyz` | VictoriaMetrics single-node + PVC, kube-state-metrics Helm chart |

### cloudflared
The cluster's only way in. Two replicas hold outbound connections to Cloudflare for the `homelab` tunnel, and `kubernetes/apps/production/cloudflared/config.yaml` maps each public hostname to its Service (generated into a hash-suffixed ConfigMap, so a change rolls the pods). The tunnel credentials are the SOPS secret beside the Deployment. Access applications and service tokens live in the Zero Trust dashboard, not in this repo.

### Kubernetes API
The control plane firewall is on with an empty allowlist, so only the cluster's own nodes and DigitalOcean reach the API server directly. `kubectl` and `flux` on a workstation go through the tunnel instead: `k8s.0x69.xyz` is a TCP route to `kubernetes.default.svc`, behind its own Access application (and cloudflared's check of that application's AUD tag, like every Access hostname). Nothing in it depends on the workstation's address.

TLS runs end to end between `kubectl` and the API server, and `kubectl` still authenticates with its DO credentials, so Access is a second gate in front of the API's own. The workstation runs a local forwarder and points its kubeconfig at it:

```bash
cloudflared access tcp --hostname k8s.0x69.xyz --url 127.0.0.1:6443   # dotfiles: k8s-api-tunnel.service
kubectl config set-cluster do-ams3-homelab-ams3 --server=https://127.0.0.1:6443 \
  --tls-server-name=<cluster id>.k8s.ondigitalocean.com
```

`--tls-server-name` keeps certificate verification against the name the API server's certificate carries. The first connection opens a browser for the Access login; the token is cached under `~/.cloudflared/` until the session expires. `doctl kubernetes cluster kubeconfig save` rewrites the server back to the public endpoint, so rerun `set-cluster` after it. To get in without the tunnel (Cloudflare down, say), add an address to the firewall for the duration: `doctl kubernetes cluster update <cluster> --enable-control-plane-firewall=true --control-plane-firewall-allowed-addresses <ip>/32`, and clear it again afterwards.

### Immich
Reachable without Access: the mobile app can't complete a browser login, so Immich's own login is the gate. Its NetworkPolicies confine the server to its database, its queue and the internet. Uses the official Helm chart with a self-managed Postgres deployment (`ghcr.io/immich-app/postgres`) that includes the Postgres extensions Immich requires. Machine learning is disabled. Library data is on a 100Gi PVC. The Postgres image version should be kept in sync with the Immich app version — check the official Immich docker-compose release for the matching tag.

### FreshRSS
Simple raw Deployment. Feeds refresh every 30 minutes via internal cron.

### Minecraft (Hearth & Hollow)
A whitelisted server for the [Hearth & Hollow](https://github.com/cosmicspork/hearth-and-hollow) modpack (`itzg/minecraft-server` with the release's server pack), scaled to zero when nobody plays:

- **mc-router** listens on the `minecraft` NodePort. While the server sleeps it answers the server list itself, with a "waking up" message while a wake is in progress. A login scales the `hearth` StatefulSet to 1 and kicks the player with a "waking up" message, and an hour with nobody connected scales it back to 0. It owns the replica count, so the StatefulSet has no `replicas` for Flux to reset.
- **The server** runs alone on the `minecraft-premium` node pool (`s-4vcpu-8gb-amd`, premium AMD for single-thread speed, autoscaled 0-1, label and taint `workload=minecraft`), so that node exists only while someone plays. From asleep, a join takes roughly the new node's boot, the volume attach, the image pull and about 30 s of server start.
- **The wake page** (`mc-wake.0x69.xyz`) shows whether the server is up and wakes it, so it boots while players launch the game. It does that by logging in to mc-router the way a client would, so it needs no API access. It sits behind Access because it is optional: joining wakes the server too, so the game itself stays reachable without a login, and only the players in the Access policy can wake it from the page.
- **DNS**: external-dns publishes `mc.0x69.xyz` (the node mc-router runs on) and the `_minecraft._tcp` SRV record for the NodePort as DNS-only Cloudflare records, and republishes them when an upgrade replaces the node.
- **Metrics**: `mc-monitor` pings the `hearth` Service each minute for VictoriaMetrics (see Monitoring): whether it answers, its response time and players online. It goes around mc-router, so its pings never keep the server awake.
- **Updates**: Renovate tracks the pack's GitHub releases through `PACK_VERSION` and never automerges them (or the itzg images): a pack release is merged once CurseForge has approved the matching client.

Outside this repo: the `minecraft-premium` node pool, the `homelab-minecraft` cloud firewall (TCP 30565 inbound on the cluster's worker tag; the DOKS-managed firewall admits nothing from outside), the Cloudflare API token in `external-dns/secret.yaml` (Zone:DNS:Edit and Zone:Zone:Read on `0x69.xyz`), the proxied `mc-wake` tunnel CNAME, and the `mc-wake` Access application (its policy lists the players).

The world lives on the `hearth-data` volume. The pod's `world-guard` init container waits until `/data/world/level.dat` exists, so an empty volume never generates a new world: to import one, copy it in with `kubectl cp <world-dir> minecraft/hearth-0:/data/world -c world-guard` while the guard is waiting. VolSync copies the whole volume to R2 every 6 hours, reading it live (see Backups): exact while the server sleeps, possibly mid-save while someone plays. The pack's own Simple Backups mod is turned off here by `simplebackups-common.toml`, which the server copies into its config on every start; its zips only duplicated that and filled the volume.

Server console commands (game rules, Chunky, `whitelist add`) go through `kubectl -n minecraft exec hearth-0 -c server -- mc-send-to-console <command>` while it is awake; output lands in the pod log.

### Monitoring
Metrics history for sizing decisions: how much each pod uses against what it reserves, how heavy tracon's harness pods are, and how often and how long the Minecraft server is awake. metrics-server only answers `kubectl top` for the current moment; this keeps 90 days.

- **VictoriaMetrics** (single-node) scrapes everything itself every 30 s and stores it on a 5Gi volume: each kubelet's cAdvisor (per-container CPU, memory, network, disk, trimmed to the series worth keeping) and resource endpoint (node totals), kube-state-metrics, and mc-monitor. Node discovery includes the `minecraft-premium` pool's node whenever it exists. It scrapes kubelets directly with only `nodes/metrics`, not through the API server's node proxy, which would need `nodes/proxy`.
- **kube-state-metrics** turns object state into series: requests and limits, restarts, phases, replica counts, and pod labels (`kube_pod_labels`, limited to `app` and the `tracon.dev/*` keys).
- **mc-monitor** lives in the `minecraft` namespace (see above).

The query UI is at `https://metrics.0x69.xyz/vmui`, behind Access. VictoriaMetrics has no login of its own; like every Access hostname, cloudflared also checks the Access token itself (`originRequest.access` in the tunnel config), so the route fails closed if the Access application is ever missing or changed. Whoever passes Access can query and ingest; the admin endpoints (delete, snapshot, reload, flags, config, pprof, cache resets) also need `?authKey=` with the key in `monitoring/secret.yaml`. A port-forward skips Access (the admin key still applies). Outside this repo: the proxied `metrics` tunnel CNAME and its Access application, whose team name and AUD tag the tunnel config must match.

To skip the tunnel:

```bash
kubectl -n monitoring port-forward svc/victoria-metrics 8428
# http://localhost:8428/vmui
```

Starting points (MetricsQL, a superset of PromQL):

```
# Node memory working set, by node pool
node_memory_working_set_bytes
# Memory used against requested, per namespace
sum by (namespace) (container_memory_working_set_bytes{container!=""})
  / sum by (namespace) (kube_pod_container_resource_requests{resource="memory"})
# Each tracon harness pod's memory, peak over the window
max_over_time(container_memory_working_set_bytes{namespace="tracon-node", container!=""}[1d])
# When the Minecraft server was awake, and who was on
kube_statefulset_replicas{namespace="minecraft", statefulset="hearth"}
minecraft_status_players_online_count
```

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
      volsync/               # Volume backups to R2 (restic mover); sources live in each app's backup.yaml
  apps/
    base/                    # Environment-agnostic base manifests
                             # (<app>/restore/: Component that fills the app's volumes from R2)
      cloudflared/
      immich/
      freshrss/
      minecraft/             # Hearth & Hollow server, mc-router, the wake page and mc-monitor
      external-dns/          # Cloudflare DNS records for NodePort Services
      monitoring/            # VictoriaMetrics, its scrape config, kube-state-metrics
    production/              # Production overlays and environment-specific resources
      cloudflared/           # Tunnel config (config.yaml): public hostname -> Service
      immich/
      freshrss/
      minecraft/
      external-dns/
      monitoring/
scripts/
  buildkit-pki.sh            # Issues tracon-build's mTLS certificates as SOPS secrets
  validate.sh                # Renders the manifests and runs the checks CI requires
.github/workflows/           # CI: manifest validation (required on main), scheduled Renovate
.sops.yaml                   # SOPS rules — which files to encrypt, with which age key
renovate.json                # Renovate config for dependency PRs
```

Flux reconciliation order: `infrastructure` → `apps` (enforced via `dependsOn`). Infrastructure has `wait: true` so all operators and CRDs are ready before apps are applied.

## Validation

`scripts/validate.sh` renders what Flux applies and fails if any Secret is not SOPS-encrypted, if a SOPS file holds more than one document (Flux decrypts objects one at a time), if an object doesn't match its schema (`kubeconform -strict`), or if a workload fails `kube-linter` (`.kube-linter.yaml`, which also requires every pod to be selected by a NetworkPolicy). The `validate` workflow runs it on every pull request and on `main`; the `main` ruleset requires it, and Renovate automerges minor and patch updates only once it passes. An object that can't meet a lint check carries an `ignore-check.kube-linter.io/<check>` annotation saying why. Run it locally with `kustomize`, `kubeconform`, `kube-linter` and `yq` on PATH. The workflow pins those tools by version and checksum, so they are bumped by hand.

Nothing in CI talks to the cluster: the DOKS control plane firewall admits no outside address (see [Kubernetes API](#kubernetes-api)). Flux fetches `main` every 5 minutes (a patch in `flux-system/kustomization.yaml`); `flux reconcile source git flux-system` through the tunnel applies a merge immediately.

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

## Backups
VolSync backs every stateful volume up to the `homelab` R2 bucket with restic, one repository per volume under `volsync/<namespace>/<pvc>`. Restic snapshots are complete point-in-time copies, but chunks are deduplicated, so a run reads only changed files and uploads only new data; a run on an idle volume costs a metadata scan.

Each app's `backup.yaml` holds its ReplicationSource and a `volsync-mover` NetworkPolicy (DNS and 443 out, nothing in); `volsync-secret.yaml` (Immich: one file per volume, since SOPS signs a whole file and Flux decrypts each object on its own) holds the repository URL, the R2 key and the restic password.

| Volume | Schedule (UTC) | Copy | Keeps |
|---|---|---|---|
| `minecraft/hearth-data` | every 6 h at :05 | Direct | 4 six-hourly, 7 daily, 4 weekly |
| `immich/immich-library` | every 6 h at :15 | Direct | 7 daily, 4 weekly, 6 monthly |
| `svastha/svastha-relay-data`, `svastha-node/svastha-node-identity` | every 6 h at :30 and :35 | Direct | 4 six-hourly, 7 daily, 4 weekly, 6 monthly |
| `pager`, `freshrss`, `tracon` (hub), `tracon-node` | daily, staggered 03:20 to 03:45 | Snapshot | 7 daily, 4 weekly, 6 monthly |
| `immich/immich-postgres-data` | daily 03:30, after Immich's own 02:00 dump into the library | Snapshot | 7 daily, 4 weekly |

Not backed up: buildkitd's cache (an emptyDir that rebuilds itself) and `monitoring/victoria-metrics-data` (losing history on a move is acceptable).

- **Consistency**: a `Snapshot` run snapshots the volume (VolumeSnapshotClass `do-block-storage`), backs up a clone of the snapshot and deletes both, so the copy is crash-consistent and the app keeps running. DigitalOcean bills the snapshot and the clone at least $0.01 each however briefly they exist, so every such run costs about $0.02 (hourly runs on seven volumes came to about $3 a day). A `Direct` run makes neither: the mover mounts the live volume on the app's node (VolSync copies the app pod's node and tolerations), so it costs nothing per run but reads files while the app may be writing them. Direct is for volumes where that is safe: write-once media, files replaced whole by rename, and a Minecraft world that is complete on disk whenever the server sleeps. Databases stay on Snapshot: SQLite (pager, FreshRSS, tracon) and Postgres can restore corrupt from a live copy, so those run once a day. Restic's 1Gi cache uses `do-block-storage`, not the retain class, so it doesn't outlive its source. Each run attaches extra volumes to the node, which is why the schedules are staggered (a DOKS node takes 15). With a single worker node a Direct mover always shares the app's node; with more, a pod rescheduled onto another node while a mover holds its volume waits for the run to finish.
- **Permissions**: in `restricted` namespaces the mover runs as the app's own user, with `fsGroup` and `OnRootMismatch` so the cache volume is writable without a recursive chown of the data. `immich` and `freshrss` (baseline, root-owned files) allow privileged movers through a namespace annotation: root with DAC_OVERRIDE, CHOWN and FOWNER, which baseline permits.
- **Pruning**: `forget` applies the retention after every run; `prune`, which repacks R2 data, runs every 7 days.

Status: `kubectl get replicationsources -A` shows each source's last sync, its duration and the next one; a failed run leaves the mover's log in `.status.latestMoverStatus`.

To browse a repository from a workstation (needs `restic`):
```bash
eval "$(sops -d kubernetes/apps/base/pager/volsync-secret.yaml |
  yq '.stringData | to_entries | .[] | "export " + .key + "=" + (.value | @sh)')"
restic snapshots
restic ls latest
```

Outside this repo: the `homelab` R2 bucket (no public access) and its R2 API token (Object Read & Write on that bucket only). The restic password is in every `volsync-secret.yaml` and, since those need the age key to read, also in Bitwarden ("homelab k8s volsync restic password", next to the age key). Without the password the backups can't be read.

### Restoring a cluster

Each backed-up app has a `restore/` Kustomize Component. It adds a ReplicationDestination per volume (a one-time manual trigger, `restore-once`) and patches `dataSourceRef` onto the app's claim. VolSync's volume populator restores the newest snapshot and only then binds the claim, so the app's pods wait in Pending until their data is in place. `scripts/validate.sh` renders every component and fails if a ReplicationSource has no matching restore.

Only a cluster whose claims don't exist yet may apply the components. Kubernetes won't add `dataSourceRef` to an existing claim, and won't remove it later either, so a cluster that was built from backups keeps its components for good. The production overlay has listed them since the ams3 cluster was built from backups; the nyc1 cluster had Flux suspended before that revision and never applied it.

To move to a new cluster, or rebuild one:

1. **Stop writes in the old cluster** if it is still running: `flux suspend kustomization apps`, scale every app to zero, then take a final backup of each volume and wait for it to finish:
   ```bash
   kubectl -n pager patch replicationsource pager-data --type merge \
     -p '{"spec":{"trigger":{"manual":"final"}}}'
   kubectl get replicationsources -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,DONE:.status.lastManualSync
   ```
   A manual trigger also stops the schedule, so nothing newer lands in R2 afterwards. Stopping the old apps matters beyond the data: the tunnel credentials would load-balance across both clusters, and two external-dns instances would fight over the records.
2. **Build the new cluster** (see [Cluster rebuild procedure](#cluster-rebuild-procedure)). The production overlay already lists each app's `restore` component next to its base. The restores start as soon as Flux applies the apps.
3. **Watch the restores.** `kubectl get replicationdestinations -A` shows each one's last sync; `kubectl get pvc -A` shows the claims binding as they finish. In the 2026-11-02 drill most volumes took under a minute, `tracon-state` about 3 minutes and `immich-library` (60 GiB) about 14.

Things learned in that drill:

- **An app that starts on an empty claim poisons `latest`.** Its next backup becomes the newest snapshot, and a later restore brings back the empty volume. The older snapshots are still there under the retention policy; restore one with `restoreAsOf` on the ReplicationDestination. The validate check exists to keep a volume from being left out.
- **Restore memory grows with file count.** `tracon-state` (510k files) was OOM-killed at 512Mi and peaked at about 1.4Gi, so its ReplicationDestination allows 2Gi. The backup still fits in 512Mi.
- **A killed or deleted mover leaves stale restic locks**, and the next backup fails at `forget` with "repository is already locked". restic doesn't expire them on its own. Set `spec.restic.unlock` on the ReplicationSource to any new value (a timestamp will do) and the next run unlocks first, or run `restic unlock` from a workstation. Don't run `restic check` against a repository while a mover is using it: it takes an exclusive lock.
- **DO's CSI driver can stall a quick re-attach.** A mover retried right after a failure waited 12 minutes on `mkfs` errors ("device does not exist") before the volume came up on its own. Allow for it before calling a restore stuck.
- **Unprivileged restores can't restore root ownership.** In `restricted` namespaces the mover runs as the app's user, so a file the snapshot has as root comes back owned by the app (group and mode are kept). It only affects a few files under `tracon-node`'s `tracon-harness-state`. `immich` and `freshrss` use privileged movers and restore ownership exactly.
- **Each ReplicationDestination keeps the VolumeSnapshot it restored from** for as long as it exists. That is about 70 GiB of DO snapshots, billed monthly. Deleting a ReplicationDestination doesn't help: Flux recreates it, and it then restores again into a new snapshot (the bound claim is untouched).

## Cluster rebuild procedure

If the cluster needs to be rebuilt from scratch:

1. **Provision a new DOKS cluster** and recreate what lives on the DigitalOcean side:
   - A `general` pool (`s-4vcpu-8gb`, 1 node) and a `minecraft-premium` pool (`s-4vcpu-8gb-amd`, falling back to `-intel` or plain `s-4vcpu-8gb` if a region lacks it; autoscaled 0 to 1, label and taint `workload=minecraft`, `NoSchedule`).
   - Auto-upgrade off; upgrades are done by hand so a node replacement never surprises a single-node cluster.
   - The control plane firewall, enabled with an empty allowlist. The `k8s` tunnel route needs no change, but each workstation's kubeconfig does: rerun the `set-cluster` command in [Kubernetes API](#kubernetes-api) with the new cluster's hostname. Until Flux has the tunnel running, `kubectl` needs a temporary allowlist entry.
   - The DOCR integration (`doctl kubernetes cluster registry add`), which puts the `cosmicspork` pull secret in every namespace. tracon-node's harness images come from the registry.
   - The new cluster's tag (`k8s:<cluster id>`) on the `homelab-minecraft` cloud firewall, in place of the old one.

   If the old cluster is still up, follow [Restoring a cluster](#restoring-a-cluster) from its first step: the old apps have to be stopped before Flux starts here.

2. **Restore your age private key** to `~/.config/sops/age/keys.txt`.

3. **Create the SOPS secret** so Flux can decrypt secrets at apply time:
   ```bash
   kubectl create namespace flux-system
   kubectl create secret generic sops-age \
     --namespace=flux-system \
     --from-file=age.agekey=$HOME/.config/sops/age/keys.txt
   ```

4. **Install Flux** from this repo's own manifests. If the old cluster is still reachable, copy its deploy key rather than making a new one:
   ```bash
   kubectl --context=<old> -n flux-system get secret flux-system -o yaml |
     yq 'del(.metadata.uid, .metadata.resourceVersion, .metadata.creationTimestamp, .metadata.managedFields, .metadata.ownerReferences)' |
     kubectl apply -f -
   kubectl apply --server-side -k kubernetes/clusters/production/flux-system
   ```
   The second apply may need running twice: the first creates the CRDs that its GitRepository and Kustomization need. Without the old cluster, `flux bootstrap github --owner=cosmicspork --repository=homelab --branch=main --path=kubernetes/clusters/production` makes a new deploy key instead; it pushes to `main` only if the Flux version differs from `gotk-components.yaml`, which branch protection would refuse, so use the same Flux CLI version.

   Flux will apply everything in order: infrastructure → apps. The tunnel reconnects on its own, since its credentials are in the repo and DNS points at the tunnel, not the cluster.

   The rest of the public edge lives in the Cloudflare account, not this repo: the `homelab` tunnel itself, the proxied DNS records (`cloudflared tunnel route dns`), and the Zero Trust Access applications, policies and service tokens. A cluster rebuild leaves them untouched. If the tunnel was deleted, create a new one, replace `secret.yaml` and the tunnel ID in the cloudflared ConfigMap, and re-run `route dns --overwrite-dns` for each hostname.

5. **Check rollout**:
   ```bash
   flux get all -A
   ```

### Notes on persistent data
The storage class `do-block-storage-retain` means DigitalOcean block volumes are **not deleted** when their PVC is deleted. On a full cluster rebuild the volumes will still exist in your DO account. To reattach them, recreate the PVs and PVCs with the matching volume IDs before Flux applies. If starting fresh, delete the old volumes in the DO console first to avoid orphaned billing. The R2 backups (see Backups) hold a copy of every stateful volume except the buildkit cache and metrics; to fill a fresh cluster's volumes from them instead, follow [Restoring a cluster](#restoring-a-cluster).

## Adding things

### A new application
1. Create `kubernetes/apps/base/<app>/` with namespace + base manifests and a `kustomization.yaml`.
2. Create `kubernetes/apps/production/<app>/` with a `kustomization.yaml` referencing the base, plus any production-specific overlays.
3. Add the app directory to `kubernetes/apps/production/kustomization.yaml`.
4. If the app needs secrets, write a `secret.yaml` and run `sops --encrypt --in-place` before committing.
5. If it has a volume worth keeping, add a `backup.yaml` and `volsync-secret.yaml` like `pager/`'s, with a schedule that doesn't collide with the others, and a `restore/` component like `pager/restore/` (validation fails without one).
6. Add a `networkpolicy.yaml`: a namespace-wide default deny, then what the app needs (see `freshrss/` for the shape). Label the namespace with the strictest Pod Security level the image meets.
7. If the app is public, add a hostname rule to the cloudflared ConfigMap, an egress rule to the app's pod in `cloudflared/networkpolicy.yaml`, run `cloudflared tunnel route dns homelab <host>.0x69.xyz`, and add an Access application in Zero Trust unless the app must be reachable without one.

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
| `cloudflared` | Tunnel DNS routes (`tunnel route dns`); needs `~/.cloudflared/cert.pem` from `cloudflared tunnel login`. Also the local forwarder to the Kubernetes API (`access tcp`) |
