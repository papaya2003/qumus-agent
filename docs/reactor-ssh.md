# Reactor over SSH (decided 2026-11-10)

Jialei chose SSH tunnels for all brain <-> workstation traffic (2026-11-10). The Olares host serves the vdW
reactor ONLY (Jialei, 2026-11-10): it is not a hub for the lab PC modules (8781/8782), which need their own
route to the brain. This page covers the reactor.

## 中文摘要

- 大脑（Mac）开一条 SSH 隧道到 Olares：本机 `127.0.0.1:18772` → Olares 的 Lab Console `127.0.0.1:8772`。
  大脑用 `ReactorConsole("http://127.0.0.1:18772/lab/")`，代码和走 VPN 时完全一样，只是地址不同。
- 隧道脚本：`REACTOR_SSH=qumus@100.64.0.1 scripts/reactor_tunnel.sh`（断了自动重连）。
- **前提**：reactor 负责人给一个 SSH 账号（只用钥匙登录，只允许转发指定端口）；Mac 能到 Olares 的 22 端口
  （一般要先连 LarePass VPN）。下面有可以直接发给负责人的请求。

## Tunnels

| From | Command (runs on) | Opens | Goes to |
|---|---|---|---|
| Brain (Mac) | `ssh -N -L 127.0.0.1:18772:127.0.0.1:8772 qumus@100.64.0.1` (Mac) | Mac `127.0.0.1:18772` | Olares Lab Console `127.0.0.1:8772` (base `/lab/`) |
| Brain (Mac), optional | add `-L 127.0.0.1:11985:127.0.0.1:1985` | Mac `127.0.0.1:11985` | reactor camera streams |

`scripts/reactor_tunnel.sh` keeps the first tunnel open (redials with backoff, `ExitOnForwardFailure`,
keepalive every 5 s). `qumus2.reactor.TUNNEL_CONSOLE_URL` is `http://127.0.0.1:18772/lab/`.

`stations.yaml` entry for the real reactor:

```yaml
reactor: {kind: real, adapter: console, url: http://127.0.0.1:18772/lab/, session: qumus}
```

Read-only check through the tunnel (GET requests only, no session, no control):

```sh
python -m qumus2.reactor.console probe --url http://127.0.0.1:18772/lab/
```

## Why the console and not the six device ports

Decided by Jialei on 2026-11-10: the tunnel goes to the console. Since reactor ADR 0010
(2026-11-09) agents control devices through the Lab Console, which holds their leases and shows each agent
holder on its Overview, where the owner can take control back with **Release all**. Tunnelling to the device
services (8766-8771) with `ReactorLab` would bypass the console and fight it for device control. One tunnel
instead of six.

## Safety notes

- Tunnelled requests arrive at Olares from `127.0.0.1`. The reactor console does not treat loopback specially,
  so nothing new opens up. (qumus-lab had this problem and fixed it in its PR #7: module ports no longer trust
  loopback.)
- Stops never need control and go through the same tunnel. If the tunnel drops, the console keeps the agent's
  leases for up to 10 minutes of idle time (reactor `docs/agent-cli.en.md`); a motion already started finishes
  as commanded. The brain therefore never starts reactor motion while it has no tunnel, and the Qumus policy
  refuses all motion and heating until Jialei allows it.

## Request to the reactor owner (copy-paste ready)

> Hi, Qumus (the stacking agent) would like to talk to the reactor through an SSH tunnel to the Lab Console,
> following ADR 0010 (agent session `agent-qumus`; read-only at first). Could you create:
>
> 1. A login `qumus` on the Olares host, SSH key only (no password), no sudo. It only needs port forwarding;
>    an interactive shell is not required.
> 2. In its `~/.ssh/authorized_keys`, the key we send you, restricted like this (OpenSSH 7.8+; Olares runs 9.6):
>    ```
>    restrict,port-forwarding,permitopen="127.0.0.1:8772",permitopen="127.0.0.1:1985" ssh-ed25519 AAAA... qumus-brain-mac
>    ```
>    This key can only reach the Lab Console (8772) and the camera streams (1985). It cannot open a shell or
>    listen on any port of Olares.
> 3. `AllowTcpForwarding yes` for this user in sshd (default on most systems).
>
> Which address should we use for SSH: the LarePass VPN address `100.64.0.1`, or is Olares reachable on the
> campus network?
