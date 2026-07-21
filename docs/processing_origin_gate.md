# Processing provider & remote-save origin gate

VolView ships processing (the Analysis/Jobs tab) and remote session save in
**every** build — the old `VITE_ENABLE_PROCESSING` / `VITE_ENABLE_REMOTE_SAVE`
build flags are gone. What a deployed client is allowed to **contact** is decided
at runtime by a single egress origin gate, so one artifact serves both the public
demo and a processing-enabled deployment (such as DSA) with no per-build config.

## The rule: same-origin only

A configured egress target — a processing provider `baseUrl`/`jobsBaseUrl` or a
remote-save `save=` URL — is allowed **if and only if its origin is same-origin
with the served client** (the deployment's own server — trusted by definition,
zero config). Cross-origin egress is never permitted; there is no allow-list
mechanism. (An earlier design read a `/processing-origins.json` allow-list; it
was removed — every deployment serves VolView same-origin with its backend.)

The gate lives in VolView (`src/io/originGate.ts`) and is enforced where each
configured target is accepted: provider registration gates **every** egress
base the provider would reach (`baseUrl` and `jobsBaseUrl`), and a cross-origin
`save=` target (or returned `resumeUrl`) is refused before it ever becomes the
save destination. This is what keeps a crafted config or a
`save=` URL param from pointing egress (a session zip, a bearer token) at a
third-party origin.

## Config trust attaches to the origin, not the channel

VolView recognizes a config **by shape**, from any import channel — the launch
`config=` URL, a dropped file, or a JSON inside the normal `urls=` file list
(`src/io/import/configJson.ts`). There is no trusted-channel distinction: a
`processing` section is honored no matter how it arrived, and its providers
register only if the origin gate passes. So a config smuggled through a data
URL cannot point the client anywhere the deployment's own origin doesn't serve.

## Same-origin deployment (DSA): nothing to configure

When the client, the backend (this Girder plugin), and the save endpoint are all
served from the same origin — the DSA topology — everything just works. The
provider config this plugin injects (`baseUrl`, `jobsBaseUrl`) and the `save=`
URL point at the deployment's own origin, which is trusted implicitly.

## Demo posture (public client)

The public demo origin serves no backend, so a processing section delivered
through any channel can register nothing that responds — and anything
cross-origin is refused outright. The processing section is inert while the
rest of the config still applies. This runtime origin gate is the **sole**
egress control (there is no deployment-layer CSP `connect-src` requirement),
with Girder ACLs behind it on a real deployment.
