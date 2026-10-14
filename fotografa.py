# -*- coding: utf-8 -*-
"""
fotografa.py — scatta la «fotografia» del verdetto meteo e la salva in verdetti/,
la cartella da cui la pagina del sito (pagina/index.html) legge. In più legge il
calendario ufficiale (scheda pubblica Golee) per sapere se i coach hanno annullato.

Chi lo lancia: GitHub Actions (.github/workflows/meteo.yml), ogni 10 minuti dalle 13:00
alle 19:59 UTC (cioè il pomeriggio italiano, con l'ora legale e senza). A ogni corsa
questo script guarda l'ORA ITALIANA e decide da solo:
  - se è l'ora di uno dei controlli di config.toml ([controlli].orari) e quel controllo
    non è ancora stato fatto oggi → CONTROLLO COMPLETO: lancia il motore, legge il
    calendario, salva tutto;
  - se è l'ora del verdetto ORIENTATIVO ([controlli].orientativo, la sera prima) e domani
    c'è almeno un gruppo → motore con --data domani, salvato come previsione;
  - altrimenti, se oggi c'è già un verdetto con dei gruppi → AGGIORNAMENTO CALENDARIO:
    rilegge solo Golee e, se qualcosa è cambiato (un allenamento sparito = annullato dai
    coach), aggiorna ultimo.json. Il verdetto meteo NON viene ricalcolato;
  - altrimenti esce senza fare niente.

Il verdetto lo calcola SOLO meteo_allenamento.py (il motore): qui lo si lancia, se ne
prende il JSON e lo si salva con qualche informazione in più per la pagina (prossimo
controllo, prossimi giorni di allenamento, calendario). Nessuna regola meteo vive qui.

Uso:
  python fotografa.py                 decide da solo (controllo, aggiornamento calendario o niente)
  python fotografa.py --forza         controllo completo adesso, a qualunque ora (prova a mano)
  python fotografa.py --prova         dice solo cosa farebbe, senza scrivere niente
  python fotografa.py --prova --ora 15:41     simula la decisione a quell'ora

Sul PC: usare --prova. Con --forza i file in verdetti/ vengono scritti anche sul PC e poi
un `git pull` li rimette com'erano su GitHub (sono prodotti dal bot, non dal PC).

Scrive in verdetti/:
  ultimo.json             la fotografia che la pagina legge
  ultimo.txt              lo stesso verdetto in testo, leggibile su GitHub dal telefono
  AAAA-MM-GG_HHMM.json    la storia dei controlli completi (HHMM = ora pianificata del
                          controllo; con --forza il nome finisce in _manuale)
Se il motore fallisce, ultimo.json porta "errore" (o, se oggi c'era già un verdetto buono,
lo tiene e aggiunge "aggiornamento_fallito") e lo script esce con codice 1: la pagina lo
dice e GitHub manda l'avviso. Solo libreria standard.
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

QUI = Path(__file__).resolve().parent
sys.path.insert(0, str(QUI))
import meteo_allenamento as motore  # noqa: E402  (carica_config, gruppi_del_giorno, scarica, minuti, GIORNI_LUNGHI)

VERDETTI = QUI / "verdetti"
# Finestra in cui una corsa «vale» per un controllo: dall'ora pianificata a 50 minuti dopo.
# Le corse partono ogni 10 minuti e GitHub può ritardarle: la prima che cade nella finestra
# fa il controllo, le altre lo trovano già fatto. Oltre i 50 minuti il controllo salta (e la
# pagina mostra l'ora vera dell'ultimo verdetto, mai un'ora inventata).
DOPO_MIN = 50
GIORNI_AVANTI = 21  # quanti giorni di calendario allenamenti (dal config) mettere nella fotografia


# ----------------------------------------------------------------------------- ora
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


def in_ora_italiana(iso):
    """'2026-10-14T16:30:00.000Z' (UTC, come lo dà Golee) -> datetime in ora italiana."""
    d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    try:
        from zoneinfo import ZoneInfo
        return d.astimezone(ZoneInfo("Europe/Rome"))
    except Exception:
        return d.astimezone()  # ora locale del sistema (Europe/Rome sul PC e su GitHub)


def in_finestra(orario, adesso):
    m = adesso.hour * 60 + adesso.minute
    return motore.minuti(orario) <= m < motore.minuti(orario) + DOPO_MIN


def controllo_in_corso(orari, adesso):
    """L'ora pianificata del controllo a cui questa corsa corrisponde, o None."""
    for o in orari:
        if in_finestra(o, adesso):
            return o
    return None


# ----------------------------------------------------------------------------- calendario
def prossimi_allenamenti(cfg, da):
    """I prossimi giorni con allenamento secondo il config (da oggi in poi), per la pagina:
    così nei giorni senza allenamento sa dire quando è il prossimo."""
    out = []
    for i in range(GIORNI_AVANTI):
        d = da + dt.timedelta(days=i)
        gruppi = motore.gruppi_del_giorno(cfg, d)
        if gruppi:
            out.append({"data": d.isoformat(), "giorno": motore.GIORNI_LUNGHI[d.weekday()],
                        "gruppi": [{"nome": g["nome"], "inizio": g["inizio"], "fine": g["fine"]} for g in gruppi]})
    return out


def leggi_calendario(cfg, oggi, adesso):
    """Gli allenamenti nel calendario pubblico Golee da oggi a `giorni_avanti` giorni
    (l'API accetta al massimo 7 giorni per chiamata: se ne fanno quante servono).
    Non si ferma mai: se Golee non risponde, torna l'errore e la pagina lo dice."""
    c = cfg.get("calendario")
    if not c:
        return None
    letto = adesso.strftime("%H:%M")
    base = c["golee_api"].format(org=c["golee_organizzazione"])
    tipi = {s.lower() for s in c.get("tipi", ["Allenamento", "practice"])}

    def e_allenamento(e):
        # Il 14/10/2026 Golee ha cambiato il campo type da "Allenamento" a "practice" senza avviso:
        # vale il tipo (lista nel config) OPPURE il titolo che comincia con "Allenamento".
        return (str(e.get("type") or "").lower() in tipi or str(e.get("title") or "").lower().startswith("allenamento")) and not e.get("allDay")
    avanti = int(c.get("giorni_avanti", 14))
    eventi, visti = [], set()
    try:
        for k in range(0, avanti, 7):
            da = oggi + dt.timedelta(days=k)
            a = min(oggi + dt.timedelta(days=k + 7), oggi + dt.timedelta(days=avanti))
            url = base + "?" + urllib.parse.urlencode({"fromDate": da.isoformat(), "toDate": a.isoformat()})
            risposta = motore.scarica(url, "calendario", timeout=30)
            if isinstance(risposta, list) and not risposta:
                time.sleep(5)  # una risposta vuota si riprova una volta prima di crederci
                risposta = motore.scarica(url, "calendario", timeout=30)
            if not isinstance(risposta, list):
                raise motore.FonteNonRisponde(f"risposta inattesa: {str(risposta)[:120]}")
            if k == 0 and not any(isinstance(e, dict) and e_allenamento(e) for e in risposta):
                # Se la settimana di oggi non ha nessun allenamento riconoscibile e la successiva sì,
                # ogni gruppo risulterebbe «annullato dai coach»: un falso annullamento. Successo il
                # 14/10/2026 alle 17:49, quando Golee ha rinominato il tipo di evento. Settimana di oggi
                # senza allenamenti = calendario non affidabile, si dice e non si annulla niente.
                motivo = "tornato vuoto" if not risposta else f"{len(risposta)} eventi ma nessuno riconosciuto come allenamento (Golee ha cambiato formato?)"
                raise motore.FonteNonRisponde(f"calendario Golee di questa settimana {motivo}: risposta non affidabile")
            for e in risposta:
                if not e_allenamento(e) or e.get("id") in visti:
                    continue
                visti.add(e.get("id"))
                ini, fin = in_ora_italiana(e["start"]), in_ora_italiana(e["end"])
                eventi.append({"data": ini.date().isoformat(), "inizio": ini.strftime("%H:%M"),
                               "fine": fin.strftime("%H:%M"), "titolo": e.get("title") or ""})
    except motore.FonteNonRisponde as e:
        return {"letto": letto, "scheda": c.get("scheda"), "eventi": [], "errore": str(e)}
    except (KeyError, TypeError, ValueError) as e:
        return {"letto": letto, "scheda": c.get("scheda"), "eventi": [], "errore": f"risposta non leggibile ({e})"}
    eventi.sort(key=lambda e: (e["data"], e["inizio"], e["titolo"]))
    return {"letto": letto, "scheda": c.get("scheda"), "eventi": eventi, "errore": None}


def _norm(s):
    return re.sub(r"\s+", "", (s or "").lower())


def applica_calendario(foto, cal, cfg):
    """Scrive in ogni verdetto cosa dice il calendario Golee («coach»).
    Gruppo tracciato = almeno una delle sue parole (golee = [...] nel config) compare in un
    allenamento di oggi o dei prossimi giorni. Per ogni parola tracciata: oggi c'è o manca.
      in_programma  tutte le parole tracciate hanno l'allenamento di oggi
      annullato     nessuna ce l'ha (i coach lo hanno tolto)
      parziale      alcune sì e alcune no (es. Under 13 c'è, Under 15 no)
      non_tracciato il calendario non conosce questo gruppo: non si dice niente"""
    foto["calendario"] = None if cal is None else {k: v for k, v in cal.items() if k != "eventi"}
    if cal is not None:
        foto["calendario"]["allenamenti_oggi"] = [e for e in cal["eventi"] if e["data"] == foto["data"]]
        foto["calendario"]["allenamenti_in_calendario"] = len(cal["eventi"])
    per_nome = {g["nome"]: g for g in cfg["gruppi"]}
    for v in foto.get("verdetti", []):
        v.pop("coach", None)
        if not cal or cal.get("errore"):
            continue
        parole = per_nome.get(v["gruppo"], {}).get("golee") or []
        if not parole:
            continue
        oggi = [e for e in cal["eventi"] if e["data"] == foto["data"]]
        tutti = cal["eventi"]
        tracciate = [p for p in parole if any(_norm(p) in _norm(e["titolo"]) for e in tutti)]
        if not tracciate:
            v["coach"] = {"stato": "non_tracciato", "letto": cal["letto"]}
            continue
        presenti, in_programma, mancanti = [], [], []
        for p in tracciate:
            ev = [e for e in oggi if _norm(p) in _norm(e["titolo"])]
            if ev:
                presenti.append(p)
                in_programma.extend(f"{e['titolo']} {e['inizio']}-{e['fine']}" for e in ev)
            else:
                mancanti.append(p)
        stato = "in_programma" if not mancanti else ("annullato" if not presenti else "parziale")
        v["coach"] = {"stato": stato, "letto": cal["letto"], "in_programma": in_programma, "annullati": mancanti}


def riga_calendario(foto):
    """Una riga di testo per ultimo.txt (chi legge da GitHub)."""
    cal = foto.get("calendario")
    if not cal:
        return ""
    if cal.get("errore"):
        return f"CALENDARIO GOLEE alle {cal['letto']}: NON LEGGIBILE ({cal['errore']}). Fa fede il gruppo WhatsApp."
    pezzi = []
    for v in foto.get("verdetti", []):
        c = v.get("coach")
        if not c or c["stato"] == "non_tracciato":
            continue
        if c["stato"] == "in_programma":
            pezzi.append(f"{v['gruppo']} in programma ({', '.join(c['in_programma'])})")
        elif c["stato"] == "annullato":
            pezzi.append(f"{v['gruppo']} ANNULLATO DAI COACH (manca in calendario)")
        else:
            pezzi.append(f"{v['gruppo']}: annullato {', '.join(c['annullati'])}; in programma {', '.join(c['in_programma'])}")
    return f"CALENDARIO GOLEE alle {cal['letto']}: " + (" · ".join(pezzi) if pezzi else "nessun gruppo di oggi è tracciato in calendario") + "."


# ----------------------------------------------------------------------------- motore e file
def lancia_motore(argomenti=()):
    """Lancia meteo_allenamento.py --json (più gli argomenti dati, es. --data per domani) e
    restituisce il suo JSON (l'ultimo blocco stampato). Il motore salva comunque il suo
    esito in esiti/, come sempre."""
    cmd = [sys.executable, str(QUI / "meteo_allenamento.py"), "--json", *argomenti]
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


def scrivi_ultimo(foto):
    VERDETTI.mkdir(exist_ok=True)
    scrivi_json(VERDETTI / "ultimo.json", foto)
    testo = foto.get("testo") or f"ERRORE: {foto.get('errore')}"
    riga = riga_calendario(foto)
    (VERDETTI / "ultimo.txt").write_text(testo + ("\n" + riga if riga else "") + "\n", encoding="utf-8")


def salva(foto, nome_storia):
    """ultimo.json e ultimo.txt per la pagina, più la copia nella storia."""
    scrivi_ultimo(foto)
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
        scrivi_ultimo(precedente)
        return "tenuto il verdetto delle %s di oggi, con l'avviso che l'aggiornamento non è riuscito" % precedente.get("controllo", "")[11:16]
    scrivi_ultimo(foto)
    return "salvato verdetti/ultimo.json con l'errore, così la pagina lo dice"


# ----------------------------------------------------------------------------- main
def controllo_orientativo(cfg, orari, adesso, ora_pianificata, prova):
    """La sera prima, il verdetto ORIENTATIVO per domani ([controlli].orientativo): motore con
    --data domani (bollettino parte «domani», modelli; niente radar) più il calendario Golee di
    domani. Sostituisce ultimo.json solo se riesce: se il motore fallisce resta il verdetto di
    oggi, che vale di più di una previsione mancata."""
    domani = adesso.date() + dt.timedelta(days=1)
    if not motore.gruppi_del_giorno(cfg, domani):
        print(f"Domani ({motore.GIORNI_LUNGHI[domani.weekday()]}) nessun gruppo si allena: niente verdetto orientativo.")
        return 0
    nome_storia = f"{domani.isoformat()}_orientativo.json"
    if (VERDETTI / nome_storia).exists():
        print(f"Il verdetto orientativo per domani è già stato fatto ({nome_storia}).")
        return 0
    if prova:
        print(f"PROVA: farei adesso il verdetto orientativo per domani {domani:%d/%m} e lo salverei come verdetti/{nome_storia}.")
        return 0
    foto = {"versione": 2, "tipo": "orientativo", "pianificato": ora_pianificata,
            "fotografato": adesso.strftime("%Y-%m-%d %H:%M"),
            "eseguito_da": "github-actions" if os.environ.get("GITHUB_ACTIONS") else "pc",
            "controlli": orari, "data": domani.isoformat()}
    print(f"Lancio il motore per il verdetto orientativo di domani {domani:%d/%m}…")
    try:
        risultato = lancia_motore(["--data", domani.isoformat()])
    except Exception as e:
        foto["errore"] = str(e)
        VERDETTI.mkdir(exist_ok=True)
        scrivi_json(VERDETTI / nome_storia, foto)
        print(f"ERRORE: {e}\nultimo.json non toccato (resta il verdetto di oggi); errore salvato in verdetti/{nome_storia}.")
        return 1
    foto.update(risultato)
    foto["prossimo_controllo"] = orari[0] if orari else None
    foto["prossimi_allenamenti"] = prossimi_allenamenti(cfg, domani)
    cal = leggi_calendario(cfg, domani, adesso)
    applica_calendario(foto, cal, cfg)
    salva(foto, nome_storia)
    riassunto = " · ".join(f"{v['gruppo']}: {v['esito']}" + (f" {v['conferma']}%" if v.get("conferma") is not None else "")
                           for v in foto.get("verdetti", []))
    print(f"Verdetto orientativo per domani fotografato alle {adesso:%H:%M} → {riassunto}")
    print(f"Salvato: verdetti/ultimo.json, verdetti/ultimo.txt, verdetti/{nome_storia}")
    return 0


def controllo_completo(cfg, orari, adesso, pianificato, nome_storia, etichetta):
    foto = {"versione": 2, "tipo": "verdetto", "pianificato": pianificato or "manuale",
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
    cal = leggi_calendario(cfg, adesso.date(), adesso)
    applica_calendario(foto, cal, cfg)
    salva(foto, nome_storia)
    riassunto = " · ".join(f"{v['gruppo']}: {v['esito']}" + (f" [coach: {v['coach']['stato']}]" if v.get("coach") else "")
                           for v in foto.get("verdetti", [])) or "oggi nessun gruppo si allena"
    print(f"Fotografato il controllo {etichetta} alle {adesso:%H:%M} → {riassunto}")
    if cal and cal.get("errore"):
        print(f"Calendario Golee NON letto: {cal['errore']}")
    print(f"Salvato: verdetti/ultimo.json, verdetti/ultimo.txt, verdetti/{nome_storia}")
    return 0


def aggiornamento_calendario(cfg, adesso, prova):
    ultimo = leggi_ultimo()
    if not ultimo or ultimo.get("data") != adesso.date().isoformat() or ultimo.get("errore") or not ultimo.get("verdetti"):
        print("Non è l'ora di un controllo e oggi non c'è un verdetto con dei gruppi: niente da fare.")
        return 0
    if prova:
        print("PROVA: rileggerei il calendario Golee e aggiornerei verdetti/ultimo.json se qualcosa è cambiato.")
        return 0
    cal = leggi_calendario(cfg, adesso.date(), adesso)
    prima = json.dumps([v.get("coach") for v in ultimo["verdetti"]], sort_keys=True)
    prima_err = (ultimo.get("calendario") or {}).get("errore")
    applica_calendario(ultimo, cal, cfg)
    dopo = json.dumps([v.get("coach") for v in ultimo["verdetti"]], sort_keys=True)
    dopo_err = (ultimo.get("calendario") or {}).get("errore")
    if prima == dopo and bool(prima_err) == bool(dopo_err):
        print(f"Calendario Golee riletto alle {adesso:%H:%M}: niente di cambiato" + (f" (non leggibile: {dopo_err})" if dopo_err else "") + ".")
        return 0
    scrivi_ultimo(ultimo)
    print(f"Calendario Golee riletto alle {adesso:%H:%M}: aggiornato verdetti/ultimo.json → " +
          " · ".join(f"{v['gruppo']}: {(v.get('coach') or {}).get('stato', '-')}" for v in ultimo["verdetti"]))
    return 0


def main():
    ap = argparse.ArgumentParser(description="Fotografa il verdetto meteo per la pagina del sito (Sharks Palermo)")
    ap.add_argument("--forza", action="store_true", help="controllo completo adesso, anche fuori orario")
    ap.add_argument("--prova", action="store_true", help="dice solo cosa farebbe, senza scrivere niente")
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
    orientativo = cfg.get("controlli", {}).get("orientativo")
    if a.forza:
        etichetta, nome_storia = "manuale", f"{adesso:%Y-%m-%d_%H%M}_manuale.json"
        pianificato = None
    elif pianificato is None and orientativo and in_finestra(orientativo, adesso):
        return controllo_orientativo(cfg, orari, adesso, orientativo, a.prova)
    elif pianificato is None:
        return aggiornamento_calendario(cfg, adesso, a.prova)
    else:
        etichetta, nome_storia = f"delle {pianificato}", f"{adesso:%Y-%m-%d}_{pianificato.replace(':', '')}.json"
        if (VERDETTI / nome_storia).exists():
            print(f"Il controllo {etichetta} di oggi è già stato fatto ({nome_storia}).")
            return aggiornamento_calendario(cfg, adesso, a.prova)
    if a.prova:
        print(f"PROVA: farei adesso il controllo {etichetta} (motore + calendario) e lo salverei come verdetti/{nome_storia}.")
        return 0
    return controllo_completo(cfg, orari, adesso, pianificato, nome_storia, etichetta)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
