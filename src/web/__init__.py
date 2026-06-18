"""Marginea web (NX-20) — gateway SSE pentru widget-ul de chat pe site.

Al treilea canal, prin ACEEAȘI margine neutră ca WhatsApp/Telegram (NX-60): intrare
`POST /web/messages` → envelope `channel_kind='webchat'` pe streamul `inbound`; ieșire
`outbox` → `WebSender` → SSE. Pipeline-ul (stagiile 3-9) rămâne agnostic de canal.
"""
