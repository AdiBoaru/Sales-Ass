-- ============================================================================
-- 039 — Citire publică (anon) pe `categories`, pentru catalogul din vitrină
-- ============================================================================
-- DE CE: vitrina (demo.nativextech.com, repo Sales-MVP-Frontend) nu putea citi
-- `categories` — RLS activ, fără nicio policy pentru `anon`, deci SELECT-ul
-- întorcea 0 rânduri (nu eroare). Ca paliativ, frontendul își DERIVA categoriile
-- din numele produsului cu ~27 de cuvinte-cheie `ilike`. Măsurat pe catalogul de
-- azi, paliativul pune 186 din 300 de produse (62%) în „Diverse", iar „Tonere" și
-- „Parfumuri" ies goale — pentru că `ilike` NU ignoră diacriticele, iar cheile
-- erau scrise fără ele („masca" nu prinde „Măști", „sampon" nu prinde „Șampoane").
-- Datele reale sunt curate: toate cele 300 de produse active au
-- `primary_category_id`, iar acestea se adună în 6 rădăcini care acoperă 100% din
-- catalog. Deci nu mai derivăm nimic — deschidem taxonomia reală, controlat.
--
-- CE EXPUNEM: DOAR categoriile care chiar poartă produse active (ele sau un
-- descendent). Din cele 102 rânduri din tabel, 38 sunt folosite + cele 6 rădăcini
-- ale lor; restul de 64 sunt rămășițe din feed și nu au ce căuta într-un meniu.
-- Filtrul ăsta trăiește în POLICY, nu în frontend: altfel fiecare consumator nou
-- (vitrină, bot, alt canal) ar trebui să reinventeze aceeași excludere.
--
-- ⚠ `path` NU e sursă de adevăr aici și NU se expune. 19 din 102 categorii au
-- `path` care contrazice `parent_id` (ex. `creme-de-zi` are
-- path='ten/ingrijirea-tenului/creme-de-zi', dar părintele lui, `ingrijirea-
-- tenului`, e rădăcină cu path='ingrijirea-tenului'). `path` a rămas din
-- taxonomia sursă, `parent_id` e cel reconstruit și referențial consistent —
-- ierarhia se calculează EXCLUSIV prin recursie pe `parent_id`. Dacă expunem
-- `path`, frontendul îl va folosi pentru breadcrumb-uri și va afișa un arbore
-- care nu există.
--
-- IDEMPOTENT: se poate rula de câte ori vrei.
-- ============================================================================

-- ── 1. Predicatul de expunere ───────────────────────────────────────────────
-- SECURITY DEFINER e OBLIGATORIU, nu comoditate: predicatul are nevoie să
-- coboare prin `categories` ca să adune descendenții, iar o policy pe
-- `categories` care interoghează `categories` sub RLS intră în recursiune
-- infinită (Postgres o respinge la runtime). Rulând ca owner, funcția ocolește
-- RLS-ul și rupe bucla. `search_path` fixat ca să nu poată fi deturnată prin
-- shadowing de schemă.
create or replace function public.category_has_active_products(cat_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  with recursive subtree as (
    select cat_id as id
    union all
    select c.id
    from categories c
    join subtree s on c.parent_id = s.id
  )
  select exists (
    select 1
    from products p
    join subtree s on s.id = p.primary_category_id
    where p.status = 'active'
  );
$$;

revoke all on function public.category_has_active_products(uuid) from public;
grant execute on function public.category_has_active_products(uuid) to anon, authenticated;

-- ── 2. Policy de citire ─────────────────────────────────────────────────────
-- Aceeași formă ca `public read active products` de pe `products` (anon +
-- authenticated, doar SELECT). Nu filtrăm pe `business_id`: policy-ul existent
-- pe `products` nu o face nici el, deci o categorie e cu exact un grad mai
-- puțin sensibilă decât produsul care o referă și pe care anon îl vede deja.
-- Dacă apare al doilea tenant cu vitrină, ASTA e linia de schimbat, împreună cu
-- `public read active products`.
alter table categories enable row level security;

drop policy if exists "public read in-use categories" on categories;
create policy "public read in-use categories" on categories
  for select
  to anon, authenticated
  using (public.category_has_active_products(id));

-- ── 3. View-ul cu numărătorile ──────────────────────────────────────────────
-- DE CE view și nu numărat din frontend: vitrina afișa badge-ul cu numărul de
-- produse făcând câte un `count` per categorie — 13 request-uri cu taxonomia
-- ghicită, 44 cu cea reală, la fiecare intrare în magazin. Aici e un singur GET.
--
-- `security_invoker = on` e deliberat: view-ul NU trebuie să fie o poartă din
-- dos peste policy-ul de mai sus. Rulează cu drepturile apelantului, deci vede
-- exact categoriile pe care anon are voie să le vadă, iar `products` rămâne
-- filtrat de `public read active products`.
--
-- `product_count` e pe SUBARBORE, nu pe rândul propriu — produsele stau uneori
-- direct pe rădăcină („Îngrijirea buzelor": 7, „Protecție solară": 6), alteori
-- pe frunze. Un count pe rândul propriu ar arăta 0 la „Machiaj", care are 101.
create or replace view public.store_categories
with (security_invoker = on) as
with recursive subtree as (
  select id as root_id, id as node_id from categories
  union all
  select s.root_id, c.id
  from subtree s
  join categories c on c.parent_id = s.node_id
)
select
  c.id,
  c.parent_id,
  c.name,
  c.slug,
  count(p.id)::int as product_count
from categories c
join subtree s on s.root_id = c.id
left join products p
  on p.primary_category_id = s.node_id
 and p.status = 'active'
group by c.id, c.parent_id, c.name, c.slug;

grant select on public.store_categories to anon, authenticated;

-- ── Verificare ──────────────────────────────────────────────────────────────
-- Ca `anon`, aștept 6 rădăcini cu 104 / 101 / 48 / 34 / 7 / 6, sumă 300:
--   select name, product_count from store_categories
--    where parent_id is null order by product_count desc;
--
-- Mulțimea e închisă în sus (părintele unei categorii folosite are, prin
-- definiție, produse în subarbore), deci `parent_id is null` chiar identifică
-- rădăcinile — frontendul nu are nevoie de altă interogare ca să afle arborele.
