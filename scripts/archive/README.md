# Arhivă — scripturi one-off istorice

`apply_003.py`–`apply_013.py` = aplicatoarele manuale ale migrărilor 003–013, înlocuite de
runnerul ordonat `scripts/migrate.py` (NX-123). Migrările în sine trăiesc în `docs/0NN_*.sql`
și sunt deja aplicate live. Păstrate doar pentru referință istorică (ex. provisioning-ul
parolei `bot_runtime` din `apply_005.py`).

`summarize_reviews_demo.py` = fostul `scripts/summarize_reviews.py`, mutat aici la NX-279. Nu
citea `reviews`: inventa rezumatele cu model și rescria `products.rating` cu variație inventată,
acceptabil doar pe recenziile FICTIVE ale demoului. Are gardă (refuză tenanții cu recenzii reale).
Producătorul real al `product_review_summaries` e `scripts/derive_review_summaries.py`.
