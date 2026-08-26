# Security

Semantic Mobile Agent can control an unlocked Android device with the same practical authority as the enabled Accessibility service, Appium session, or ADB user. Treat the service as privileged infrastructure.

## Required deployment controls

- Do not expose port `8787`, Appium `4723`, or the ADB server directly to the public Internet.
- Bind to a private management network, VPN, loopback interface, or authenticated reverse proxy.
- Use a separate cloud-phone account and least-privilege test data whenever possible.
- Configure `SMA_BRIDGE_TOKEN` and the matching device-side secure setting for the Accessibility bridge.
- Keep final payment, order, ride, message, destructive, publishing, booking, and authorization confirmation enabled.
- Restrict which parent agents can call the MCP server; stdio transport should run as a child process of the trusted host.
- Rotate LLM/API credentials and never store them in plans, events, screenshots, or Git.
- Collect audit events, but redact message bodies, addresses, account identifiers, and other personal data before centralized logging.

## Out of scope

The project must not be used to bypass captchas, biometric checks, payment passwords, anti-abuse controls, account ownership checks, or an application's authorization model.

## Reporting

For a private vulnerability report, use the repository owner's private contact channel rather than publishing active device credentials or exploit details in a public issue.
