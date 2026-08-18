"""NX-249 — controllerul de release: cine primește v2, cu ce dovezi, și cum se dă înapoi.

Patru module, fiecare cu o singură responsabilitate:

  • `models`       — contractele imutabile (`ReleasePolicy`, `Assignment`, `CapturedExecution`,
                     `DecisionRecord`) + tabelul etapelor de rollout;
  • `assignment`   — bucketing HMAC determinist + semantica de epoch (procentul atinge DOAR
                     conversațiile noi);
  • `policy_store` — citire validată + CAS + audit + fail-closed pe `force_control`;
  • `gates`/`report` — verdictele pe artefacte deja produse de NX-241/246/247/248.

Nimic de aici nu promovează singur trafic: `PASS` e o constatare, `apply` e o decizie umană.
"""
