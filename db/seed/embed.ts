/**
 * embed-products.ts — generează embeddings pentru products.ai_summary
 * și le scrie în product_embeddings. Idempotent: sare peste produsele
 * al căror content_hash nu s-a schimbat.
 *
 * Rulare:  npx tsx src/embed-products.ts
 * .env:    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, OPENAI_API_KEY
 *          EMBEDDING_MODEL=text-embedding-3-small   (1536 dim, match vector(1536))
 */
import { createClient } from "@supabase/supabase-js";
import { createHash } from "node:crypto";

const MODEL = process.env.EMBEDDING_MODEL ?? "text-embedding-3-small";
const BATCH = 64;

function requiredEnv(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`Missing ${name}`);
  return value;
}

const sha = (s: string) => createHash("sha256").update(s).digest("hex");

async function embedBatch(texts: string[]): Promise<number[][]> {
  const res = await fetch("https://api.openai.com/v1/embeddings", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${requiredEnv("OPENAI_API_KEY")}`
    },
    body: JSON.stringify({ model: MODEL, input: texts })
  });
  if (!res.ok) throw new Error(`OpenAI ${res.status}: ${await res.text()}`);
  const json = await res.json();
  return json.data.map((d: { embedding: number[] }) => d.embedding);
}

async function main() {
  const supabase = createClient(requiredEnv("SUPABASE_URL"), requiredEnv("SUPABASE_SERVICE_ROLE_KEY"), {
    auth: { persistSession: false }
  });

  // produsele active cu ai_summary + hash-ul embeddings existente
  const { data: products, error } = await supabase
    .from("products")
    .select("id, business_id, ai_summary")
    .eq("status", "active")
    .not("ai_summary", "is", null);
  if (error) throw error;

  const { data: existing } = await supabase
    .from("product_embeddings")
    .select("product_id, content_hash, model");
  const existingByProduct = new Map((existing ?? []).map((e: any) => [e.product_id, e]));

  const pending = (products ?? []).filter((p: any) => {
    const cur = existingByProduct.get(p.id);
    return !cur || cur.model !== MODEL || cur.content_hash !== sha(p.ai_summary);
  });

  console.log(`Products: ${products?.length ?? 0}, need (re)embedding: ${pending.length}`);

  for (let i = 0; i < pending.length; i += BATCH) {
    const chunk = pending.slice(i, i + BATCH);
    const vectors = await embedBatch(chunk.map((p: any) => p.ai_summary));
    const rows = chunk.map((p: any, j: number) => ({
      product_id: p.id,
      business_id: p.business_id,
      model: MODEL,
      embedding: vectors[j],
      content_hash: sha(p.ai_summary),
      updated_at: new Date().toISOString()
    }));
    const { error: upErr } = await supabase
      .from("product_embeddings")
      .upsert(rows, { onConflict: "product_id" });
    if (upErr) throw upErr;
    console.log(`embedded ${Math.min(i + BATCH, pending.length)}/${pending.length}`);
  }

  console.log("Done.");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
