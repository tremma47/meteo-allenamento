# Meteo allenamento — Sharks Palermo

«Oggi si fa allenamento o no?» Risposta automatica per i gruppi della Sharks Football
ASD, calcolata sul punto del campo Paolo Borsellino (Palermo) da tre fonti pubbliche:
il bollettino di criticità della Protezione Civile, cinque modelli di previsione
(Open-Meteo) e il radar (RainViewer). Nessuna chiave, nessun segreto.

La pagina che i genitori e i coach leggono è
**www.sharkspalermofootball.com/meteo/**. L'indicazione è automatica: la conferma
arriva sempre dai coach sul gruppo WhatsApp. Fulmine visto o sentito = tutti fuori
dal campo, qualunque sia il verdetto.

## Cosa c'è qui

- `meteo_allenamento.py` — il motore, unico: legge le fonti e applica la regola.
- `config.toml` — campo, gruppi, orari, soglie (anche quelle del consiglio di abbigliamento):
  **l'unico posto** dove si cambiano.
- `fotografa.py` — agli orari dei controlli lancia il motore e salva il verdetto in
  `verdetti/` (`ultimo.json` per la pagina, `ultimo.txt` per leggerlo da qui, più la storia);
  ogni 10 minuti nel pomeriggio rilegge il calendario ufficiale dell'associazione (la scheda
  pubblica su Golee): un allenamento che i coach annullano sparisce dal calendario, e la
  pagina lo dice.
- `.github/workflows/meteo.yml` — lo fa girare sui server di GitHub ogni giorno nel
  pomeriggio (controlli alle 15:30 e alle 17:30 italiane) e alle 23:00 per il verdetto
  orientativo dell'indomani, senza bisogno di un PC acceso.
- `pagina/index.html` — la pagina del sito, che legge `verdetti/ultimo.json`.
- `pubblica_pagina.sh` — carica la pagina sul sito (una tantum, a mano).
- `Meteo-Allenamento.bat` — sul PC: doppio click per il controllo di adesso.

Verdetti possibili per gruppo: **SI FA · ANNULLATO · NON PREVEDIBILE** (sulla pagina:
allenamento confermato / annullato / da confermare). «Non prevedibile» è il verdetto onesto
dei giorni di temporali (le celle nascono sul posto) e dei giorni in cui una fonte manca:
non diventa mai un via libera. Se i coach tolgono l'allenamento dal calendario, la pagina
mostra «annullato dai coach» qualunque sia il meteo. La «conferma» in percentuale è 100 meno
la probabilità di pioggia nella fascia (scenari ensemble di Open-Meteo): misura la pioggia,
non i temporali, e va letta insieme al verdetto.
