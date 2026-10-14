# -*- coding: utf-8 -*-
"""
fotografa.py — scatta la «fotografia» del verdetto meteo e la salva in verdetti/,
la cartella da cui la pagina del sito (pagina/index.html) legge.

Chi lo lancia: GitHub Actions (.github/workflows/meteo.yml), quattro volte al giorno
alle 13:30, 14:30, 15:30 e 16:30 UTC. Quattro perché GitHub ragiona in UTC e l'ora
legale sposta l'Italia di un'ora: questo script guarda l'ORA ITALIANA e lavora solo
se è l'ora di uno dei controlli scritti in config.toml ([controlli].orari) e quel
controllo non è già stato fatto oggi. Negli altri casi esce senza fare niente.

Il verdetto lo calcola SOLO meteo_allenamento.py (il motore): qui lo si lancia, se ne
prende il JSON e lo si salva con qualche informazione in più per la pagina (prossimo
controllo, prossimi giorni di allenamento). Nessuna regola meteo vive in questo file.

Uso:
  python fotografa.py                 controllo, se è l'ora di uno dei controlli
  python fotografa.py --forza         controllo adesso, a qualunque ora (prova a mano)
  python fotografa.py --prova         dice solo cosa farebbe, senza lanciare il motore
  python fotografa.py --prova --ora 15:41     simula la decisione a quell'ora

Scrive in verdetti/:
  ultimo.json             la fotografia che la pagina legge
  ultimo.txt              lo stesso verdetto in testo, leggibile su GitHub dal telefono
  AAAA-MM-GG_HHMM.json    la storia (HHMM = ora pianificata del controllo; con --forza
                          il nome finisce in _manuale)
Se il motore fallisce, ultimo.json contiene il campo "errore" e lo script esce con
codice 1: la pagina dice che il controllo non è riuscito e GitHub manda l'avviso.
Solo libreria standard.
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

QUI = Path(__file__).resolve().parent
sys.path.insert(0, str(QUI))
import meteo_allenamento as motore  # noqa: E402  (carica_config, gruppi_del_giorno, minuti, GIORNI_LUNGHI)

VERDETTI = QUI / "verdetti"
# Finestra in cui una corsa «vale» per un controllo: da 10 minuti prima a 50 minuti
# dopo l'ora pianificata. GitHub può far partire la corsa in ritardo di qualche
# decina di minuti; oltre i 50 il controllo salta (e la pagina mostra l'ora vera
# dell'ultimo verdetto, mai un'ora inventata).
PRIMA_MIN, DOPO_MIN = 10, 50
GIORNI_AVANTI = 21  # quanti giorni di calendario allenamenti mettere nella fotografia


def ora_italiana_ok(adesso):
    """L'orologio locale deve essere in ora italiana (su GitHub lo garantisce
    TZ=Europe/Rome nel workflow). Torna (va_bene, messaggio)."""
    try:
        from zoneinfo import ZoneInfo
        roma = dt.datetime.now(ZoneInfo("Europe/Rome")).replace(tzinfo=None)
    except Exception as e:  # senza dati dei fusi non si può verificare: si va avanti dicendolo
        return True, f"fuso non verificabile ({e}): assumo ora italiana"
    if abs((roma - adesso).total_seconds()) > 90:
        return False, f"l'orologio locale ({adesso:%H:%M}) non è in ora italiana ({roma:%H:%M}): imposta TZ=Europe/Rome"
    return True, "ora italiana"


def controllo_in_corso(orari, adesso):
    """L'ora pianificata del controllo a cui questa corsa corrisponde, o None."""
    m = adesso.hour * 60 + adesso.minute
    for o in orari:
        if motore.minuti(o) - PRIMA_MIN <= m < motore.minuti(o) + DOPO_MIN:
            return o
    return None


def prossimi_allenamenti(cfg, da):
    """I prossimi giorni con allenamento (da oggi in poi), per la pagina: così nei
    giorni senza allenamento sa dire quando è il prossimo."""
    out = []
    for i in range(GIORNI_AVANTI):
        d = da + dt.timedelta(days=i)
        gruppi = motore.gruppi_del_giorno(cfg, d)
        if gruppi:
            out.append({"data": d.isoformat(), "giorno": motore.GIORNI_LUNGHI[d.weekday()],
                        "gruppi": [{"nome": g["nome"], "inizio": g["inizio"], "fine": g["fine"]} for g in gruppi]})
    return out


def lancia_motore():
    """Lancia meteo_allenamento.py --json e restituisce il suo JSON (l'ultimo blocco
    stampato). Il motore salva comunque il suo esito in esiti/, come sempre."""
    cmd = [sys.executable, str(QUI / "meteo_allenamento.py"), "--json"]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(QUI))
    if p.returncode != 0:
        raise RuntimeError(f"il motore è uscito con codice {p.returncode}: {(p.stderr or p.stdout).strip()[-600:]}")
    out = p.stdout
    i = out.rfind("\n{\n")
    if i < 0:
        raise RuntimeError("il motore non ha stampato il JSON: " + out.strip()[-600:])
    return json.loads(out[i + 1:])


def scrivi_json(percorso, oggetto):
    percorso.write_text(json.dumps(oggetto, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def leggi_ultimo():
    try:
        return json.loads((VERDETTI / "ultimo.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def salva(foto, nome_storia):
    """ultimo.json e ultimo.txt per la pagina, più la copia nella storia."""
    VERDETTI.mkdir(exist_ok=True)
    scrivi_json(VERDETTI / "ultimo.json", foto)
    (VERDETTI / "ultimo.txt").write_text(foto.get("testo") or f"ERRORE: {foto.get('errore')}", encoding="utf-8")
    scrivi_json(VERDETTI / nome_storia, foto)


def salva_errore(foto, nome_storia):
    """Il motore ha fallito. Se oggi c'era già un verdetto buono (per esempio quello delle
    15:30 quando fallisce l'aggiornamento delle 17:30) la pagina deve continuare a
    mostrarlo, con l'avviso che l'aggiornamento non è riuscito: non lo si cancella con
    un errore. Altrimenti ultimo.json porta l'errore e la pagina lo dice."""
    VERDETTI.mkdir(exist_ok=True)
    scrivi_json(VERDETTI / nome_storia, foto)
    precedente = leggi_ultimo()
    if precedente and precedente.get("data") == foto["data"] and not precedente.get("errore") and precedente.get("verdetti"):
        precedente["aggiornamento_fallito"] = {"pianificato": foto["pianificato"], "ora": foto["fotografato"], "errore": foto["errore"]}
        scrivi_json(VERDETTI / "ultimo.json", precedente)
        return "tenuto il verdetto delle %s di oggi, con l'avviso che l'aggiornamento non è riuscito" % precedente.get("controllo", "")[11:16]
    scrivi_json(VERDETTI / "ultimo.json", foto)
    (VERDETTI / "ultimo.txt").write_text(f"ERRORE: {foto['errore']}", encoding="utf-8")
    return "salvato verdetti/ultimo.json con l'errore, così la pagina lo dice"


def main():
    ap = argparse.ArgumentParser(description="Fotografa il verdetto meteo per la pagina del sito (Sharks Palermo)")
    ap.add_argument("--forza", action="store_true", help="fa il controllo adesso, anche fuori orario")
    ap.add_argument("--prova", action="store_true", help="dice solo cosa farebbe, senza lanciare il motore")
    ap.add_argument("--ora", help="HH:MM, solo con --prova: simula la decisione a quell'ora")
    a = ap.parse_args()
    if a.ora and not a.prova:
        ap.error("--ora vale solo insieme a --prova")

    try:
        cfg = motore.carica_config(QUI / "config.toml")
    except Exception as e:
        print(f"ERRORE in config.toml: {e}")
        return 2
    orari = list(cfg.get("controlli", {}).get("orari", []))
    adesso = dt.datetime.now().replace(second=0, microsecond=0)
    ok, msg = ora_italiana_ok(adesso)   # si verifica l'orologio vero, anche quando si simula un'ora
    if a.ora:
        h, m = map(int, a.ora.split(":"))
        adesso = adesso.replace(hour=h, minute=m)
    print(f"Adesso: {motore.GIORNI_LUNGHI[adesso.weekday()]} {adesso:%d/%m/%Y %H:%M} ({msg}). Controlli previsti: {', '.join(orari) or 'nessuno'}.")
    if not ok:
        print("FERMO: non fotografo niente con un orologio sbagliato.")
        return 2

    pianificato = controllo_in_corso(orari, adesso)
    if a.forza:
        etichetta, nome_storia = "manuale", f"{adesso:%Y-%m-%d_%H%M}_manuale.json"
    elif pianificato is None:
        print("Non è l'ora di un controllo: niente da fare.")
        return 0
    else:
        etichetta, nome_storia = f"delle {pianificato}", f"{adesso:%Y-%m-%d}_{pianificato.replace(':', '')}.json"
        if (VERDETTI / nome_storia).exists():
            print(f"Il controllo {etichetta} di oggi è già stato fatto ({nome_storia}): niente da fare.")
            return 0
    if a.prova:
        print(f"PROVA: farei adesso il controllo {etichetta} e lo salverei come verdetti/{nome_storia}.")
        return 0

    foto = {"versione": 1, "pianificato": pianificato or "manuale",
            "fotografato": adesso.strftime("%Y-%m-%d %H:%M"),
            "eseguito_da": "github-actions" if os.environ.get("GITHUB_ACTIONS") else "pc",
            "controlli": orari, "data": adesso.date().isoformat()}
    print(f"Lancio il motore per il controllo {etichetta}…")
    try:
        risultato = lancia_motore()
    except Exception as e:
        foto["errore"] = str(e)
        print(f"ERRORE: {e}\n" + salva_errore(foto, nome_storia) + ".")
        return 1
    foto.update(risultato)
    m_adesso = adesso.hour * 60 + adesso.minute
    foto["prossimo_controllo"] = next((o for o in orari if motore.minuti(o) > m_adesso), None)
    foto["prossimi_allenamenti"] = prossimi_allenamenti(cfg, adesso.date())
    salva(foto, nome_storia)
    riassunto = " · ".join(f"{v['gruppo']}: {v['esito']}" for v in foto.get("verdetti", [])) or "oggi nessun gruppo si allena"
    print(f"Fotografato il controllo {etichetta} alle {adesso:%H:%M} → {riassunto}")
    print(f"Salvato: verdetti/ultimo.json, verdetti/ultimo.txt, verdetti/{nome_storia}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
