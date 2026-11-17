import { APIResponse } from '@playwright/test';

// Parse a girder REST response as JSON, failing with a bounded excerpt of the
// body — the one HTTP-error convention for the whole e2e helper suite.
export async function readJson(res: APIResponse, ctx: string): Promise<any> {
  const status = res.status();
  const text = await res.text();
  if (status >= 300) {
    throw new Error(`[e2e] ${ctx} failed: HTTP ${status} ${text.slice(0, 400)}`);
  }
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(`[e2e] ${ctx}: non-JSON response: ${text.slice(0, 200)}`);
  }
}
