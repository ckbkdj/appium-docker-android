# Hardened deployment example

The REST process has no built-in multi-tenant identity system. Keep it on a private interface and put authentication at the control plane boundary.

Example systemd environment:

```dotenv
SMA_HOST=127.0.0.1
SMA_PORT=8787
SMA_REQUIRE_CONFIRMATION=true
SMA_ALLOW_UNSAFE=false
SMA_BRIDGE_TOKEN=<random device-matched token>
```

Then expose it only through your existing authenticated Agent gateway or private VPN. The MCP stdio entry point is preferable when Lobster can launch the child process directly, because it does not create a listening network port.

Never publish ADB `5037`, Appium `4723`, the bridge forwarded port, or REST `8787` to an untrusted network.
