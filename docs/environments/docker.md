# Docker in the sandbox

How a [heavy](heavy-tier.md) sandbox runs Docker: the storage, cgroups and daemon hangar sets up at claim. Then how
Docker's image pulls get through the sandbox's egress policy.

## At claim

At claim, before the repo's services, hangar installs `~/.hangar/bin/hangar-docker`, a standard-library script like
the [supervisor](supervisor.md). It runs it as root with sudo (heavy environments need `sudo: true`):

1. **Storage.** dockerd's overlay storage can't use virtio-fs as its upper layer, and under Kata the sandbox's volume
   for `/var/lib/docker` is virtio-fs. So there it makes a sparse ext4 image of the profile's disk on that volume and
   loop-mounts it on `/var/lib/docker`.
    - That is the approach Kata's own guide recommends over a memory-backed volume
      ([How to run Docker in Docker with Kata Containers][kata-dind]).
    - On ext4, xfs, btrfs or tmpfs (a microVM's root disk, a Docker volume) it uses the directory as it is.
    - The volume is an `emptyDir` sized to the disk (`sizeLimit`), with an `ephemeral-storage` request of the same. So
      the scheduler places the pod where the node's disk has room.
2. **cgroups.** Kata's guest uses cgroup v1 unless its kernel is told otherwise ([kata-agent][kata-agent]). dockerd 29
   still supports it, though deprecated ([Docker deprecations][docker-deprecated]). On cgroup v2 it moves the
   container's processes into a child cgroup first, so controllers can be delegated (what moby's `hack/dind` does).
3. **dockerd** comes from `HANGAR_HEAVY_DOCKER_IMAGE` (`docker:29.8.2-dind`), copied into the image as static binaries
   with the CLI, Compose and Buildx. It runs under a loop that restarts it, with iptables for its NAT and published
   ports. The bridge MTU is set to the sandbox's, and its socket is owned by the sandbox user's group. Nothing listens
   on TCP.

Its result is recorded (`get_sandbox`'s `info.heavy.status`) and shown to the agent. If Docker doesn't start, the
sandbox still starts, with a setup failure that says why and how to start it again. Docker's images and volumes live
in the sandbox: a resumed session starts with an empty Docker.

## Docker images

dockerd's pulls go through the sandbox's egress policy like everything else:

- **Open** egress: straight to the registries. `HANGAR_HEAVY_REGISTRY_MIRRORS` (dockerd's `registry-mirrors`, Docker
  Hub only) can name `https://mirror.gcr.io`. That is Google's cache of popular Docker Hub images, whose pulls don't
  count against Docker Hub's rate limits ([Pull cached Docker Hub images][ar-mirror]).
- **Allowlist**: dockerd uses the sandbox's egress proxy, with its own credential. The `docker` preset opens Docker
  Hub (`registry-1.docker.io`, `auth.docker.io` and its blob CDN, from [Docker's allowlist][docker-allowlist]). Other
  registries go in `allow`. Docker Hub also takes pushes with anyone's token, so prefer hangar's mirror.
- **hangar's mirror** (`HANGAR_HEAVY_MIRROR=true`): a read-only pull-through of Docker Hub on the git gateway's port.
  It follows the pattern of the [package mirrors](../security/package-mirrors.md): GET and HEAD of manifests, blobs and
  tags only.

The mirror's upstream is Docker Hub, or an Artifact Registry remote repository for Docker Hub
(`HANGAR_HEAVY_MIRROR_UPSTREAM`, terraform's `dockerhub_mirror`). That repository holds Docker Hub credentials of your
own, so a team's pulls don't hit the anonymous limit. hangar reads it with its own Google identity. Sandboxes never get
`*.pkg.dev`, which serves every project's writable repositories too.

dockerd can't send credentials to a mirror: it refuses a mirror URL with a user in it. So the sandbox's egress
credential is in the mirror's path. Use is summed into the `egress.allow` audit entries as `docker.mirror.hangar`.

In an allowlist sandbox, containers and builds get the egress proxy too (`~/.docker/config.json`'s `proxies`, by
address: they can't resolve hangar's names).

Compose stacks run under the supervisor: see
[Compose stacks](supervisor.md#compose-stacks-under-the-service-supervisor).

[kata-dind]: https://github.com/kata-containers/kata-containers/blob/main/docs/how-to/how-to-run-docker-with-kata.md
[kata-agent]: https://github.com/kata-containers/kata-containers/blob/main/src/agent/README.md
[docker-deprecated]: https://docs.docker.com/engine/misc/deprecated/
[ar-mirror]: https://docs.cloud.google.com/artifact-registry/docs/pull-cached-dockerhub-images
[docker-allowlist]: https://docs.docker.com/desktop/setup/allow-list/
