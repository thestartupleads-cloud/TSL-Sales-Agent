// Cloudflare Cron Trigger for Clearcoat. Does not contain API tokens or customer data.
export default {
  async scheduled(_event, env) {
    if (!env.CLEARCOAT_SYNC_URL || !env.CLEARCOAT_SYNC_SECRET) {
      throw new Error("Clearcoat scheduler bindings are not configured");
    }

    const endpoint = new URL(env.CLEARCOAT_SYNC_URL);
    if (endpoint.protocol !== "https:" || endpoint.hostname !== "clearcoat-api.onrender.com") {
      throw new Error("Unexpected Clearcoat sync destination");
    }

    const response = await fetch(endpoint.toString(), {
      method: "POST",
      headers: { "X-Sync-Secret": env.CLEARCOAT_SYNC_SECRET },
      redirect: "manual",
      signal: AbortSignal.timeout(120000),
    });
    if (!response.ok) {
      // Only log the status, never request headers or GHL message bodies.
      throw new Error(`Clearcoat incremental sync returned HTTP ${response.status}`);
    }

    const result = await response.json();
    if (result.status !== "completed") {
      throw new Error("Clearcoat incremental sync did not complete");
    }

    console.log(JSON.stringify({
      status: "completed",
      streams_completed: (result.streams || []).length,
      messages_inserted: (result.streams || []).reduce((count, stream) => count + stream.messages_inserted, 0),
    }));
  },
};
