// github-dispatch — thin, purpose-scoped proxy that lets the MTD dashboard
// trigger the `fetch_mtd.yml` GitHub Action and poll its run status, without
// ever exposing a GitHub token to the browser.
//
// Replaces the old Cloudflare Worker (storage-dispatch.*.workers.dev) whose
// personal GitHub PAT was revoked. The token now lives ONLY as the Supabase
// secret GH_DISPATCH_TOKEN and never leaves the server.
//
// The board calls this with the Supabase anon key (verify_jwt stays ON), e.g.
//   supabase.functions.invoke('github-dispatch', { body: { op: 'dispatch' } })
//
// Ops (POST JSON body):
//   { op: 'dispatch' }               -> workflow_dispatch on ref main
//   { op: 'latest_run' }             -> { id } of the most recent run
//   { op: 'run_status', runId: N }   -> { status, conclusion }

const REPO = "Robbosd/storage-reporting";
const WORKFLOW = "fetch_mtd.yml";
const GH_API = "https://api.github.com";

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...cors, "Content-Type": "application/json" },
  });
}

// Every GitHub REST call needs Authorization + Accept + a User-Agent.
function ghHeaders(token: string): HeadersInit {
  return {
    "Authorization": `Bearer ${token}`,
    "Accept": "application/vnd.github+json",
    "User-Agent": "storage-mtd-dispatch",
    "X-GitHub-Api-Version": "2022-11-28",
  };
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  if (req.method !== "POST") return json({ error: "POST only" }, 405);

  const token = Deno.env.get("GH_DISPATCH_TOKEN");
  if (!token) return json({ error: "GH_DISPATCH_TOKEN not configured" }, 500);

  let body: { op?: string; runId?: number };
  try {
    body = await req.json();
  } catch {
    return json({ error: "invalid JSON body" }, 400);
  }

  const op = body.op;
  try {
    if (op === "dispatch") {
      const r = await fetch(
        `${GH_API}/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
        {
          method: "POST",
          headers: { ...ghHeaders(token), "Content-Type": "application/json" },
          body: JSON.stringify({ ref: "main" }),
        },
      );
      if (!r.ok) {
        return json({ error: `dispatch failed: ${r.status}`, detail: await r.text() }, 502);
      }
      return json({ ok: true });
    }

    if (op === "latest_run") {
      const r = await fetch(
        `${GH_API}/repos/${REPO}/actions/workflows/${WORKFLOW}/runs?per_page=1`,
        { headers: ghHeaders(token) },
      );
      if (!r.ok) return json({ error: `runs failed: ${r.status}` }, 502);
      const d = await r.json();
      return json({ id: d.workflow_runs?.[0]?.id ?? null });
    }

    if (op === "run_status") {
      if (!body.runId) return json({ error: "runId required" }, 400);
      const r = await fetch(`${GH_API}/repos/${REPO}/actions/runs/${body.runId}`, {
        headers: ghHeaders(token),
      });
      if (!r.ok) return json({ error: `run_status failed: ${r.status}` }, 502);
      const run = await r.json();
      return json({ status: run.status, conclusion: run.conclusion });
    }

    return json({ error: `unknown op: ${op}` }, 400);
  } catch (e) {
    return json({ error: String(e) }, 500);
  }
});
