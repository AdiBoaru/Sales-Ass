/**
 * seed-v2.ts — încarcă data/demo/catalog.json (cele 500 produse existente)
 * în schema v2 multi-tenant.
 *
 * Diferențe față de seed-supabase.ts (v1):
 *  - creează/refolosește tenant-ul (businesses) și pune business_id peste tot
 *  - onConflict compus: business_id,slug / business_id,sku
 *  - product_ingredients folosește ingredient_id (FK), nu ingredient_name
 *  - product_images nu mai are coloana `kind`
 *  - generează un ai_summary determinist per produs (suficient pt embeddings)
 *  - availability derivat din stocul variantelor
 *
 * Rulare:  npx tsx src/seed-v2.ts
 * .env:    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
 *          SEED_BUSINESS_SLUG=sole-demo  SEED_BUSINESS_NAME="Sole Demo"
 */
import "dotenv/config";
import { createClient } from "@supabase/supabase-js";
import { getConfig } from "./config.js";
import type { DemoCatalog, DemoProduct, SourceProduct } from "./types.js";
import { readJson } from "./utils.js";

type IdBySlug = Map<string, string>;
type LooseSupabase = any;

function requiredEnv(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`Missing ${name}. Add it to .env before running seed.`);
  return value;
}

async function getOrCreateBusiness(supabase: LooseSupabase): Promise<string> {
  const slug = process.env.SEED_BUSINESS_SLUG ?? "sole-demo";
  const name = process.env.SEED_BUSINESS_NAME ?? "Sole Demo";
  const { data, error } = await supabase
    .from("businesses")
    .upsert({ slug, name, vertical: "ecommerce" }, { onConflict: "slug" })
    .select("id")
    .single();
  if (error) throw error;
  console.log(`Tenant: ${slug} → ${data.id}`);
  return data.id;
}

async function upsertBrands(supabase: LooseSupabase, businessId: string, catalog: DemoCatalog): Promise<IdBySlug> {
  const rows = catalog.brands.map((b) => ({ ...b, business_id: businessId }));
  const { data, error } = await supabase
    .from("brands")
    .upsert(rows, { onConflict: "business_id,slug" })
    .select("id, slug");
  if (error) throw error;
  return new Map((data ?? []).map((row: { slug: string; id: string }) => [row.slug, row.id]));
}

async function upsertCategories(supabase: LooseSupabase, businessId: string, catalog: DemoCatalog): Promise<IdBySlug> {
  const bySlug = new Map(catalog.categories.map((c) => [c.slug, c]));
  const depthOf = (slug: string | undefined): number => {
    let depth = 0;
    let current = slug ? bySlug.get(slug) : undefined;
    const seen = new Set<string>();
    while (current?.parentSlug && !seen.has(current.slug)) {
      seen.add(current.slug);
      depth += 1;
      current = bySlug.get(current.parentSlug);
    }
    return depth;
  };
  const pathOf = (slug: string): string => {
    const parts: string[] = [];
    let current = bySlug.get(slug);
    const seen = new Set<string>();
    while (current && !seen.has(current.slug)) {
      seen.add(current.slug);
      parts.unshift(current.slug);
      current = current.parentSlug ? bySlug.get(current.parentSlug) : undefined;
    }
    return parts.join("/");
  };

  const sorted = [...catalog.categories].sort((a, b) => depthOf(a.slug) - depthOf(b.slug));
  const idBySlug = new Map<string, string>();

  for (const category of sorted) {
    const payload = {
      business_id: businessId,
      name: category.name,
      slug: category.slug,
      path: pathOf(category.slug),
      parent_id: category.parentSlug ? idBySlug.get(category.parentSlug) ?? null : null
    };
    const { data, error } = await supabase
      .from("categories")
      .upsert(payload, { onConflict: "business_id,slug" })
      .select("id, slug")
      .single();
    if (error) throw error;
    idBySlug.set(data.slug, data.id);
  }
  return idBySlug;
}

function ingredientSlug(name: string): string {
  return name
    .toLowerCase()
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/(^-|-$)/g, "");
}

async function upsertIngredients(
  supabase: LooseSupabase,
  businessId: string,
  catalog: DemoCatalog
): Promise<Map<string, string>> {
  // Two different names can produce the same slug (e.g. "Aloe Vera" vs "+/- Aloe Vera").
  // Deduplicate by slug before upserting so PostgreSQL doesn't see two rows conflicting
  // on the same (business_id, slug) within a single batch command.
  const canonicalBySlug = new Map<string, string>(); // slug → first-seen name
  const slugByName = new Map<string, string>();        // name → slug (all variants)
  for (const product of catalog.products) {
    for (const ing of product.ingredients) {
      if (!ing.name) continue;
      const s = ingredientSlug(ing.name);
      if (!canonicalBySlug.has(s)) canonicalBySlug.set(s, ing.name);
      slugByName.set(ing.name, s);
    }
  }

  const idByCanonical = new Map<string, string>();
  const entries = [...canonicalBySlug.entries()]; // [slug, canonical_name]
  for (let i = 0; i < entries.length; i += 500) {
    const chunk = entries.slice(i, i + 500).map(([slug, name]) => ({ business_id: businessId, name, slug }));
    const { data, error } = await supabase
      .from("ingredients")
      .upsert(chunk, { onConflict: "business_id,slug" })
      .select("id, name");
    if (error) throw error;
    for (const row of data ?? []) idByCanonical.set(row.name, row.id);
  }

  // Build a full lookup covering all name variants → id so product_ingredients FKs resolve.
  const idByName = new Map<string, string>();
  for (const [name, s] of slugByName.entries()) {
    const canonical = canonicalBySlug.get(s)!;
    const id = idByCanonical.get(canonical);
    if (id) idByName.set(name, id);
  }

  console.log(`Seeded ${canonicalBySlug.size} ingredients.`);
  return idByName;
}

/** Sumar determinist pe care rulezi apoi embeddings; îl poți înlocui ulterior cu unul LLM. */
function buildAiSummary(product: DemoProduct, categoryNames: string[]): string {
  const attrs = Object.entries(product.attributes)
    .filter(([k]) => !k.startsWith("source_") && k !== "original_brand_replaced")
    .map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : v}`)
    .slice(0, 8);
  const keyIngredients = product.ingredients.filter((i) => i.isKey).map((i) => i.name).slice(0, 6);
  const parts = [
    product.name,
    categoryNames.length ? `Categorie: ${categoryNames.join(" > ")}` : "",
    product.shortDescription ?? "",
    product.badges.length ? `Badge-uri: ${product.badges.join(", ")}` : "",
    attrs.length ? attrs.join(" · ") : "",
    keyIngredients.length ? `Ingrediente cheie: ${keyIngredients.join(", ")}` : "",
    `Preț: ${product.salePrice ?? product.price} ${product.currency}`
  ];
  return parts.filter(Boolean).join("\n");
}

function deriveAvailability(product: DemoProduct): string {
  const total = product.variants.reduce((s, v) => s + (v.stock ?? 0), 0);
  if (total <= 0) return "out_of_stock";
  if (total <= 3) return "low_stock";
  return "in_stock";
}

async function upsertRawSourceProducts(supabase: LooseSupabase, businessId: string, rawOutput: string) {
  let products: SourceProduct[];
  try {
    products = await readJson<SourceProduct[]>(rawOutput);
  } catch {
    console.warn(`Raw source file not found, skipping source_products_raw: ${rawOutput}`);
    return;
  }
  for (let i = 0; i < products.length; i += 100) {
    const chunk = products.slice(i, i + 100).map((p) => ({
      business_id: businessId,
      source_site: p.sourceSite,
      source_url: p.sourceUrl,
      scraped_at: p.scrapedAt,
      payload: p
    }));
    const { error } = await supabase
      .from("source_products_raw")
      .upsert(chunk, { onConflict: "business_id,source_url" });
    if (error) throw error;
  }
  console.log(`Seeded ${products.length} raw source products.`);
}

async function replaceProductChildren(
  supabase: LooseSupabase,
  businessId: string,
  productId: string,
  product: DemoProduct,
  categoryIds: string[],
  ingredientIdByName: Map<string, string>
) {
  await Promise.all([
    supabase.from("product_category_map").delete().eq("product_id", productId),
    supabase.from("product_images").delete().eq("product_id", productId),
    supabase.from("product_variants").delete().eq("product_id", productId),
    supabase.from("product_sections").delete().eq("product_id", productId),
    supabase.from("product_ingredients").delete().eq("product_id", productId),
    supabase.from("product_badges").delete().eq("product_id", productId),
    supabase.from("reviews").delete().eq("product_id", productId)
  ]);

  const writes = [];

  if (categoryIds.length > 0) {
    writes.push(
      supabase.from("product_category_map").insert(
        categoryIds.map((categoryId, position) => ({ product_id: productId, category_id: categoryId, position }))
      )
    );
  }

  if (product.images.length > 0) {
    writes.push(
      supabase.from("product_images").insert(
        product.images.map((image) => ({
          product_id: productId,
          url: image.url,
          alt: image.alt,
          position: image.position
        }))
      )
    );
  }

  if (product.variants.length > 0) {
    writes.push(
      supabase.from("product_variants").insert(
        product.variants.map((v) => ({
          business_id: businessId,
          product_id: productId,
          label: v.label,
          sku: v.sku,
          price: v.price,
          sale_price: v.salePrice ?? null,
          stock: v.stock,
          color_hex: v.colorHex ?? null,
          attributes: v.attributes
        }))
      )
    );
  }

  if (product.sections.length > 0) {
    writes.push(
      supabase.from("product_sections").insert(
        product.sections.map((s, position) => ({
          product_id: productId,
          kind: s.kind,
          title: s.title ?? "",
          body: s.body,
          position
        }))
      )
    );
  }

  const ingredientRows = product.ingredients
    .map((ing) => {
      const ingredientId = ingredientIdByName.get(ing.name);
      if (!ingredientId) return null;
      return { product_id: productId, ingredient_id: ingredientId, position: ing.position, is_key: ing.isKey };
    })
    .filter(Boolean);
  if (ingredientRows.length > 0) {
    writes.push(supabase.from("product_ingredients").insert(ingredientRows));
  }

  if (product.badges.length > 0) {
    writes.push(
      supabase.from("product_badges").insert(product.badges.map((label) => ({ product_id: productId, label })))
    );
  }

  if (product.reviews.length > 0) {
    writes.push(
      supabase.from("reviews").insert(
        product.reviews.map((r) => ({
          business_id: businessId,
          product_id: productId,
          source: "demo",
          author: r.author,
          rating: r.rating,
          body: r.body,
          created_at: r.createdAt
        }))
      )
    );
  }

  const results = await Promise.all(writes);
  for (const result of results) {
    if (result.error) throw result.error;
  }
}

async function main() {
  const config = getConfig();
  const catalog = await readJson<DemoCatalog>(config.catalogOutput);
  const supabase = createClient(requiredEnv("SUPABASE_URL"), requiredEnv("SUPABASE_SERVICE_ROLE_KEY"), {
    auth: { persistSession: false }
  });

  const businessId = await getOrCreateBusiness(supabase);
  await upsertRawSourceProducts(supabase, businessId, config.rawOutput);

  const brandIds = await upsertBrands(supabase, businessId, catalog);
  const categoryIds = await upsertCategories(supabase, businessId, catalog);
  const ingredientIdByName = await upsertIngredients(supabase, businessId, catalog);

  const categoryNameBySlug = new Map(catalog.categories.map((c) => [c.slug, c.name]));

  let count = 0;
  for (const product of catalog.products) {
    const brandId = brandIds.get(product.brandSlug);
    const primaryCategoryId = product.primaryCategorySlug ? categoryIds.get(product.primaryCategorySlug) : undefined;
    if (!brandId) throw new Error(`Missing brand id for ${product.brandSlug}`);

    const categoryNames = product.categorySlugs
      .map((slug) => categoryNameBySlug.get(slug))
      .filter(Boolean) as string[];

    const { data, error } = await supabase
      .from("products")
      .upsert(
        {
          business_id: businessId,
          slug: product.slug,
          name: product.name,
          brand_id: brandId,
          primary_category_id: primaryCategoryId ?? null,
          short_description: product.shortDescription,
          description: product.description,
          ai_summary: buildAiSummary(product, categoryNames),
          currency: product.currency,
          price: product.price,
          sale_price: product.salePrice ?? null,
          availability: deriveAvailability(product),
          stock_total: product.variants.reduce((s, v) => s + (v.stock ?? 0), 0),
          rating: product.rating,
          review_count: product.reviewCount,
          status: product.status,
          source_fingerprint: product.sourceFingerprint,
          attributes: product.attributes
        },
        { onConflict: "business_id,slug" }
      )
      .select("id")
      .single();
    if (error) throw error;

    const productCategoryIds = product.categorySlugs
      .map((slug) => categoryIds.get(slug))
      .filter(Boolean) as string[];
    await replaceProductChildren(supabase, businessId, data.id, product, productCategoryIds, ingredientIdByName);

    count += 1;
    if (count % 25 === 0 || count === catalog.products.length) {
      console.log(`[${count}/${catalog.products.length}] seeded ${product.name}`);
    }
  }

  console.log("Done. Next: npx tsx src/embed-products.ts");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
