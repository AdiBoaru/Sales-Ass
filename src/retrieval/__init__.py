"""NX-238 — portul de retrieval: un contract stabil, două implementări, un singur selector.

`src.retrieval` NU conține SQL și NU e un al doilea motor de căutare. Este stratul subțire prin
care agentul (NX-239) cere candidați fără să știe CINE îi produce: traseul live canonic de azi
(`CurrentLiveRetrievalAdapter`) sau candidatul `search_entities` (`SearchEntitiesAdapter`), care
rămâne inert până la un GO semnat. Alegerea e server-side (`selector.py`) — nici modelul, nici
frontendul nu ating providerul.
"""
