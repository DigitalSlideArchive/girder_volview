# Processing provider & remote-save origin allow-list

VolView ships processing (the Analysis/Jobs tab) and remote session save in
**every** build — the old `VITE_ENABLE_PROCESSING` / `VITE_ENABLE_REMOTE_SAVE`
build flags are gone. What a deployed client is allowed to **contact** is decided
at runtime by a single egress origin gate, so one artifact serves both the public
demo and a processing-enabled deployment (such as DSA) with no per-build config.

## The rule

A configured egress target — a processing provider `baseUrl` or a remote-save
`save=` URL — is allowed if and only if its **origin** is:

- **same-origin** as the served client (the deployment's own server — trusted by
  definition, **zero config**), or
- listed in an **allow-list file the deployment serves at a fixed same-origin
  path**.

Missing / empty / unreachable allow-list ⇒ **same-origin only**. "Default-deny"
means a _cross-origin_ target never passes unless the deployment says so — it does
**not** mean the feature is off until a file is served. A same-origin facade
registers with zero configuration.

The allow-list is read **only** from the deployment-controlled same-origin path,
never from a URL parameter, a remote manifest, or a field inside an imported
config — otherwise a config could allow-list itself.

## Same-origin deployment (DSA): nothing to configure

When the client, the facade (this Girder plugin), and the save endpoint are all
served from the same origin — the DSA topology — **no allow-list file is needed**.
The provider config (its `baseUrl`) and the `save=` URL point at the deployment's
own origin, which is trusted implicitly.

## Cross-origin targets: serve `processing-origins.json`

To allow the client to reach a provider or save endpoint on a **different**
origin, serve a JSON file at the client web root:

```
/processing-origins.json
```

```json
{
  "origins": ["https://facade.example", "https://backup.example:8443"]
}
```

- Each entry is an **origin** (`scheme://host[:port]`). A bare `host` /
  `host:port` without a scheme is assumed to be `https`.
- A bare JSON array (`["https://facade.example"]`) is also accepted.
- The file must be served from the **same origin** as the client so a crafted
  `?config=` / `?urls=` cannot substitute a forged list.

## Demo posture (public client)

The public demo serves **no** allow-list, so a processing section delivered
through a URL (`?urls=`, a dropped file, or a launch manifest) can register
**nothing** cross-origin, and the demo origin serves no facade — the processing
section is inert while the rest of the config still applies. This runtime origin
gate is the **sole** egress control (there is no deployment-layer CSP
`connect-src` requirement), with Girder ACLs behind it on a real deployment.
