"""Job de LIFECYCLE (Val3, CONV-COMMERCE) — scrie `contacts.lifecycle` determinist (nocturn).

Gap din analiză: lifecycle nu era scris NICIODATĂ → toți contactii rămâneau `new`, iar segmentarea
(proactiv / RFM / raportare) era oarbă. Aici un singur UPDATE determinist clasifică fiecare contact
din semnale care EXISTĂ deja: nr. de comenzi (`orders`) + recența ultimei interacțiuni
(`conversations.last_inbound_at`). ZERO LLM (P2).

Stări (CHECK schema): `new → engaged → customer → repeat → churn_risk`.
  • churn_risk = OVERRIDE: oricine a interacționat dar e tăcut de > prag = țintă de re-engagement.
  • repeat = ≥2 comenzi · customer = 1 comandă · engaged = activitate dar 0 comenzi · new = rest.

Job ADMIN, cross-tenant (ca `rollup_usage`): comenzile/conversațiile unui contact sunt ale
ACELUIAȘI tenant (FK), deci `contact_id` (uuid global) e cheia sigură fără business_id în join.
Sub RLS (`bot_runtime` + app.business_id, ex. în teste) același SQL e auto-scoped la un tenant.
Non-PII (P12): citește doar COUNT-uri + recență, scrie doar eticheta de segment. Idempotent
(`is distinct from` → nu rescrie rândurile neschimbate). Sare contactele șterse GDPR (`erased_at`).
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# `$1` = pragul de churn (zile). `make_interval` e folosit deja în outbox/initiators (Postgres).
_LIFECYCLE_SQL = """
update contacts c
   set lifecycle = sub.lc, updated_at = now()
  from (
    select c2.id,
      case
        when la.last_inbound is not null
             and la.last_inbound < now() - make_interval(days => $1) then 'churn_risk'
        when coalesce(o.cnt, 0) >= 2 then 'repeat'
        when coalesce(o.cnt, 0) = 1 then 'customer'
        when la.last_inbound is not null then 'engaged'
        else 'new'
      end as lc
    from contacts c2
    left join (
      select contact_id, count(*) as cnt from orders where contact_id is not null
      group by contact_id
    ) o on o.contact_id = c2.id
    left join (
      select contact_id, max(last_inbound_at) as last_inbound from conversations
      group by contact_id
    ) la on la.contact_id = c2.id
    where c2.erased_at is null
  ) sub
 where c.id = sub.id and c.lifecycle is distinct from sub.lc
"""


async def run_lifecycle(conn, *, churn_days: int) -> int:
    """Reclasifică `contacts.lifecycle` pentru toți contactii. Întoarce nr. de rânduri schimbate.

    `conn` = admin_conn în prod (cross-tenant). Un singur UPDATE determinist (P2)."""
    result = await conn.execute(_LIFECYCLE_SQL, churn_days)
    # asyncpg `execute` întoarce tag-ul comenzii, ex. „UPDATE 12" → numărul de rânduri.
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError, AttributeError):
        return 0
