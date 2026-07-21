// Deployment config — a committed literal (no environment variables).
export const CONFIG = {
  // girder origin serving the VolView dist (/static/built/plugins/volview/).
  baseURL: 'http://localhost:8080',
  // girder API mount (e.g. 'girder/api/v1' behind a prefix).
  apiRoot: 'api/v1',
  // girder login; the token is also planted as the `girderToken` cookie.
  user: 'admin',
  pass: 'password',
} as const;

// Absolute girder REST URL for an api path (path must start with '/').
export const apiUrl = (path: string) => `${CONFIG.baseURL}/${CONFIG.apiRoot}${path}`;
