# Real lab drivers (stage 3)

Ported from v1 `common/hardware/` in stage 3. Each driver implements an
interface from `qumus2/devices/base.py`. Ports and paths come from
`config/devices.real.yaml`, never hard-coded (no `COM7`, no `C:\Users\...`).

Connecting to or driving real hardware needs Jialei's explicit OK first.
