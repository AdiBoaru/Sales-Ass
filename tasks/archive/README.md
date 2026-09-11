# Arhivă — carduri închise sau obsolete

- **T021–T033** — DDL-ul schemei v1 („002: …"), înlocuit integral de
  `docs/schema_v2_production.sql` (sursa de adevăr a schemei). Obsolete, nu se mai lucrează.

- **NX-61, NX-62, NX-63, NX-89, R2, NX-71, T013, T014, T015** — canalele WhatsApp și Telegram
  (inbound/outbound, onboarding, randare bogată, carusel, poarta de template + fereastra 24h,
  setup-ul manual Meta). **Șterse din proiect de NX-289 (2026-09-10)**, nu doar înghețate:
  cod, schemă (`docs/051_drop_frozen_channels.sql`), servicii de compose, variabile de mediu.
  Fiecare card poartă un banner care spune ce livra și de ce nu se mai implementează.
  Ce a rămas din zona lor (seam-ul de canal NX-60, poarta de consent) e în `tasks/NX-289.md`.
