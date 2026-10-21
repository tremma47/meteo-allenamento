# -*- coding: utf-8 -*-
"""
meteo_allenamento.py — «Oggi si fa allenamento o no?» per la Sharks Palermo ASD.

Legge tre fonti sul punto del campo Paolo Borsellino e applica a ogni gruppo che
si allena nel giorno chiesto la regola scritta in config.toml:
  1. BOLLETTINO di criticità della Protezione Civile (allerta temporali / idro),
     dal repository GitHub ufficiale pcm-dpc;
  2. PREVISIONI di 5 modelli meteo via Open-Meteo (mm di pioggia nella fascia,
     codice meteo, CAPE, potenziale fulmini di ICON-2I, confronto con la corsa di ieri);
  3. RADAR RainViewer (pioggia reale attorno al campo nell'ultima ora).
I FULMINI non sono leggibili in automatico: il testo rimanda alla mappa.

Verdetti possibili per gruppo: SI FA · ANNULLATO · NON PREVEDIBILE.
«Non prevedibile» non diventa mai un via libera: è il verdetto onesto dei giorni
di temporali (le celle nascono sul posto) e dei giorni in cui una fonte manca.

Uso:
  python meteo_allenamento.py                      verdetto per i gruppi di oggi
  python meteo_allenamento.py --data 2026-10-12 --ora 17:30     simulazione
  python meteo_allenamento.py --gruppi tutti       tutti i gruppi, anche se oggi non si allenano
  python meteo_allenamento.py --json               stampa anche il risultato in JSON
  python meteo_allenamento.py --simula-guasto radar      prova di una fonte che non risponde
                               (anche: modelli, bollettino; ripetibile)
  python meteo_allenamento.py --no-salva           non scrivere in esiti/

Ogni esecuzione salva il testo in esiti/AAAA-MM-GG_HHMM.txt (+ .json) e in
ultimo_esito.txt. Solo libreria standard; Pillow (già nel Python 3.13 di sistema)
serve per il radar: se manca, il radar risulta «non disponibile» e lo dice.
Niente chiavi, niente segreti.
"""
import argparse
import datetime as dt
import io
import json
import math
import re
import struct
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

QUI = Path(__file__).resolve().parent
GIORNI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
GIORNI_LUNGHI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
UA = "SharksPalermo-MeteoAllenamento/1.0 (sharkspalermofootball@gmail.com)"
LIVELLI = {"NESSUNA": 0, "GIALLA": 1, "ARANCIONE": 2, "ROSSA": 3, "SCONOSCIUTA": 1}
CODICI_TEMPORALE = {95, 96, 99}
RISCHI = {"temporali": "Per rischio temporali", "idrogeologico": "Per rischio idrogeologico",
          "idraulico": "Per rischio idraulico"}
GUASTI = set()  # fonti da far fallire di proposito (--simula-guasto)


class FonteNonRisponde(Exception):
    """Una fonte non ha risposto o ha risposto male: si dichiara, non si nasconde."""


# ----------------------------------------------------------------------------- rete
def scarica(url, fonte, timeout=60, binario=False):
    if fonte in GUASTI:
        raise FonteNonRisponde("guasto simulato con --simula-guasto")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            dati = r.read()
    except urllib.error.HTTPError as e:
        corpo = e.read().decode("utf-8", "replace")[:160].replace("\n", " ")
        raise FonteNonRisponde(f"HTTP {e.code} {corpo}".strip()) from None
    except Exception as e:  # timeout, DNS, rete assente
        raise FonteNonRisponde(str(e) or type(e).__name__) from None
    if binario:
        return dati
    try:
        return json.loads(dati)
    except ValueError:
        raise FonteNonRisponde("risposta non è JSON") from None


# ----------------------------------------------------------------------------- tempo e testo
def minuti(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def ore_fascia(inizio, fine):
    """Etichette orarie di Open-Meteo che coprono la fascia. Il valore delle HH:00
    è la pioggia caduta nell'ora PRECEDENTE: 18:30-20:00 -> 19:00 (18-19) e 20:00 (19-20)."""
    prima = minuti(inizio) // 60 + 1
    ultima = math.ceil(minuti(fine) / 60)
    return [f"{h:02d}:00" for h in range(prima, min(ultima, 23) + 1)]


def ore_segnali(inizio, fine, ore_prima=2):
    """Finestra più larga per i segnali di temporale: le celle che bagnano alle 18:30
    nascono nelle due ore prima."""
    h0 = max(minuti(inizio) // 60 - ore_prima, 0)
    ultima = math.ceil(minuti(fine) / 60)
    return [f"{h:02d}:00" for h in range(h0 + 1, min(ultima, 23) + 1)]


def it_mm(x):
    return "n.d." if x is None else f"{x:.1f}".replace(".", ",")


def n_modelli(n):
    return f"{n} modello" if n == 1 else f"{n} modelli"


# ----------------------------------------------------------------------------- config
def carica_config(percorso):
    with open(percorso, "rb") as f:
        cfg = tomllib.load(f)
    for chiave in ("campo", "gruppi", "soglie", "fonti"):
        if chiave not in cfg:
            raise ValueError(f"config.toml: manca la sezione [{chiave}]")
    for g in cfg["gruppi"]:
        for k in ("nome", "regola", "giorni", "inizio", "fine"):
            if k not in g:
                raise ValueError(f"config.toml: al gruppo {g.get('nome', '?')} manca '{k}'")
        if g["regola"] not in ("piccoli", "grandi"):
            raise ValueError(f"config.toml: regola '{g['regola']}' sconosciuta (piccoli/grandi)")
        for giorno in g["giorni"]:
            if giorno not in GIORNI:
                raise ValueError(f"config.toml: giorno '{giorno}' sconosciuto (lun..dom)")
    return cfg


def gruppi_del_giorno(cfg, data, tutti=False):
    out = []
    for g in cfg["gruppi"]:
        if tutti:
            out.append(g)
            continue
        if g.get("dal") and data < g["dal"]:
            continue
        if g.get("al") and data > g["al"]:
            continue
        if GIORNI[data.weekday()] in g["giorni"]:
            out.append(g)
    return out


# ----------------------------------------------------------------------------- modelli
VARIABILI = ("precipitation", "weather_code", "cape", "lightning_potential", "wind_gusts_10m", "temperature_2m",
             "precipitation_probability")


def previsioni(cfg, data):
    """Per ogni modello: valori orari del giorno. Un modello che non risponde non
    ferma gli altri: finisce in `errori` con il motivo."""
    f, c = cfg["fonti"], cfg["campo"]
    base = {"latitude": c["lat"], "longitude": c["lon"], "timezone": "Europe/Rome",
            "start_date": data.isoformat(), "end_date": data.isoformat(),
            "hourly": ",".join(VARIABILI)}
    ris, errori = {}, {}
    for m in f["modelli"]:
        url = f["open_meteo"] + "?" + urllib.parse.urlencode({**base, "models": m["id"]})
        try:
            d = scarica(url, "modelli")
            h = d["hourly"]
            ore = {}
            for i, t in enumerate(h["time"]):
                if t[:10] == data.isoformat():
                    ore[t[11:]] = {k: (h[k][i] if k in h else None) for k in VARIABILI}
            if not ore:
                raise FonteNonRisponde("nessun dato per il giorno chiesto")
            ris[m["id"]] = {"nome": m["nome"], "griglia": (d.get("latitude"), d.get("longitude")), "ore": ore}
        except FonteNonRisponde as e:
            errori[m["id"]] = f"{m['nome']}: {e}"
        except (KeyError, TypeError) as e:
            errori[m["id"]] = f"{m['nome']}: risposta incompleta ({e})"
    return ris, errori


def corsa_precedente(cfg, data, ore):
    """Quanta pioggia dava IERI il modello di riferimento per la stessa fascia di oggi.
    Un modello che cambia idea sta dicendo che non lo sa."""
    f, c = cfg["fonti"], cfg["campo"]
    p = {"latitude": c["lat"], "longitude": c["lon"], "timezone": "Europe/Rome",
         "start_date": data.isoformat(), "end_date": data.isoformat(),
         "models": f["modello_riferimento"], "hourly": "precipitation,precipitation_previous_day1"}
    d = scarica(f["previous_runs"] + "?" + urllib.parse.urlencode(p), "modelli")
    h = d["hourly"]
    idx = {t[11:]: i for i, t in enumerate(h["time"]) if t[:10] == data.isoformat()}
    oggi = sum((h["precipitation"][idx[o]] or 0) for o in ore if o in idx)
    ieri = sum((h["precipitation_previous_day1"][idx[o]] or 0) for o in ore if o in idx)
    return oggi, ieri


def mm_fascia(modello, ore):
    """Somma dei mm nella fascia; None se manca anche una sola ora."""
    valori = [modello["ore"].get(o, {}).get("precipitation") for o in ore]
    if any(v is None for v in valori):
        return None
    return sum(valori)


# ----------------------------------------------------------------------------- bollettino
def livello(testo):
    m = re.search(r"ALLERTA\s+(GIALLA|ARANCIONE|ROSSA)", testo or "", re.I)
    if m:
        return m.group(1).upper()
    if re.search(r"NESSUNA ALLERTA", testo or "", re.I):
        return "NESSUNA"
    return "SCONOSCIUTA"


def _dentro(pt, anello):
    x, y = pt
    dentro = False
    n = len(anello)
    for i in range(n):
        x1, y1 = anello[i][0], anello[i][1]
        x2, y2 = anello[(i + 1) % n][0], anello[(i + 1) % n][1]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-12) + x1:
            dentro = not dentro
    return dentro


def _zona(gj, campo):
    """La zona di allerta del campo: prima per nome, in riserva per coordinate."""
    per_nome = [f for f in gj["features"] if f["properties"].get("Nome zona") == campo["zona_allerta"]]
    if per_nome:
        return per_nome[0]["properties"], None
    pt = (campo["lon"], campo["lat"])
    for f in gj["features"]:
        g = f["geometry"]
        poligoni = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        for p in poligoni:
            if _dentro(pt, p[0]) and not any(_dentro(pt, buco) for buco in p[1:]):
                nome = f["properties"].get("Nome zona")
                return f["properties"], f"zona '{campo['zona_allerta']}' non trovata nel bollettino: usata per coordinate '{nome}' (aggiorna config.toml)"
    raise FonteNonRisponde("nessuna zona del bollettino contiene il campo")


def _ultimo_bollettino(repo):
    """Nome dell'ultimo bollettino pubblicato (AAAAMMGG_HHMM), letto dall'indice in coda a
    files/all/latest_all.zip con una richiesta parziale (8 KB invece di 4,6 MB).
    Niente API GitHub: nessun limite di richieste."""
    url = f"https://raw.githubusercontent.com/{repo}/master/files/all/latest_all.zip"
    if "bollettino" in GUASTI:
        raise FonteNonRisponde("guasto simulato con --simula-guasto")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=-16384"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            coda = r.read()[-16384:]
    except urllib.error.HTTPError as e:
        raise FonteNonRisponde(f"HTTP {e.code} su latest_all.zip") from None
    except Exception as e:
        raise FonteNonRisponde(str(e) or type(e).__name__) from None
    nomi = set()
    for m in re.finditer(rb"PK\x01\x02", coda):
        i = m.start()
        if i + 46 > len(coda):
            continue
        n_len = struct.unpack("<H", coda[i + 28:i + 30])[0]
        nome = coda[i + 46:i + 46 + n_len].decode("utf-8", "replace")
        t = re.match(r"(\d{8}_\d{4})_", nome)
        if t:
            nomi.add(t.group(1))
    if not nomi:
        raise FonteNonRisponde("indice di latest_all.zip senza nomi di bollettino")
    return max(nomi)


def _nomi_bollettini_api(repo):
    """Tutti i bollettini pubblicati, dall'API GitHub (git trees). Serve solo per le
    simulazioni su date passate: 60 richieste l'ora per indirizzo, due per chiamata."""
    radice = scarica(f"https://api.github.com/repos/{repo}/git/trees/master", "bollettino")
    sha = next((t["sha"] for t in radice["tree"] if t["path"] == "files"), None)
    if not sha:
        raise FonteNonRisponde("repository senza cartella files/")
    albero = scarica(f"https://api.github.com/repos/{repo}/git/trees/{sha}", "bollettino")
    return sorted(t["path"][:-5] for t in albero["tree"] if re.fullmatch(r"\d{8}_\d{4}\.json", t["path"]))


def bollettino(cfg, data, adesso):
    """Il bollettino valido per `data`: quello emesso lo stesso giorno (parte 'oggi')
    se è già uscito, altrimenti quello del giorno prima (parte 'domani').
    Per oggi e domani basta l'ultimo pubblicato (senza API); per le simulazioni su
    date passate, o per un'ora precedente all'uscita di oggi, si usa l'elenco completo."""
    repo = cfg["fonti"]["dpc_repo"]
    oggi = dt.date.today()
    scelta, ultimo = None, None
    if data >= oggi:
        ultimo = _ultimo_bollettino(repo)
        emesso = dt.datetime.strptime(ultimo, "%Y%m%d_%H%M")
        if emesso.date() == data and emesso <= adesso:
            scelta = (ultimo, "today")
        elif emesso.date() == data - dt.timedelta(days=1):
            scelta = (ultimo, "tomorrow")
        elif data > oggi and emesso.date() < data - dt.timedelta(days=1):
            vigilia = data - dt.timedelta(days=1)
            raise FonteNonRisponde(f"il bollettino che copre il {data:%d/%m} esce il {vigilia:%d/%m} nel pomeriggio (di solito entro le 14:35, mai dopo le 16:10): ricontrolla dopo. Ultimo pubblicato: {ultimo}")
    if not scelta:
        nomi = _nomi_bollettini_api(repo)

        def emissioni(giorno):
            pref = giorno.strftime("%Y%m%d")
            return [n for n in nomi if n.startswith(pref)]

        for n in reversed(emissioni(data)):
            if dt.datetime.strptime(n, "%Y%m%d_%H%M") <= adesso:
                scelta = (n, "today")
                break
        if not scelta:
            em = emissioni(data - dt.timedelta(days=1))
            if em:
                scelta = (em[-1], "tomorrow")
        if not scelta:
            ultimo = nomi[-1] if nomi else "-"
            raise FonteNonRisponde(f"nessun bollettino per il {data:%d/%m} né per il giorno prima (ultimo pubblicato: {ultimo})")
    nome, parte = scelta
    gj = _geojson(cfg, repo, nome, parte)
    prop, avviso = _zona(gj, cfg["campo"])
    livelli = {k: livello(prop.get(v)) for k, v in RISCHI.items()}
    sconosciuti = [f"{k}: «{prop.get(v)}»" for k, v in RISCHI.items() if livelli[k] == "SCONOSCIUTA"]
    if sconosciuti:
        avviso = ((avviso + " · ") if avviso else "") + "livello non riconosciuto, trattato come GIALLA: " + "; ".join(sconosciuti)
    return {"nome": nome, "emesso": dt.datetime.strptime(nome, "%Y%m%d_%H%M"), "parte": parte,
            "zona": prop.get("Nome zona"), "livelli": livelli,
            "testo": {k: prop.get(v) for k, v in RISCHI.items()}, "avviso": avviso}


def _geojson(cfg, repo, nome, parte):
    cache = QUI / "cache"
    cache.mkdir(exist_ok=True)
    file = cache / f"{nome}_{parte}.json"
    if file.exists():
        try:
            return json.loads(file.read_text(encoding="utf-8"))
        except ValueError:
            file.unlink()
    url = f"https://raw.githubusercontent.com/{repo}/master/files/geojson/{nome}_{parte}.json"
    dati = scarica(url, "bollettino", timeout=120, binario=True)
    try:
        gj = json.loads(dati)
    except ValueError:
        raise FonteNonRisponde("il GeoJSON del bollettino non si legge") from None
    file.write_bytes(dati)
    # pulizia: i bollettini vecchi di più di 10 giorni non servono più (5 MB l'uno)
    for vecchio in cache.glob("*.json"):
        if vecchio != file and dt.datetime.now().timestamp() - vecchio.stat().st_mtime > 10 * 86400:
            vecchio.unlink(missing_ok=True)
    return gj


# ----------------------------------------------------------------------------- radar
def radar(cfg, data, adesso):
    """Pioggia reale attorno al campo (RainViewer, mosaico radar): per gli ultimi
    fotogrammi quanti pixel di pioggia entro 10/25/50/100 km e l'eco più vicina."""
    if data != dt.date.today():
        raise FonteNonRisponde("il radar esiste solo per oggi: RainViewer tiene le ultime 2 ore")
    try:
        from PIL import Image
    except ImportError:
        raise FonteNonRisponde("Pillow non installato nel Python in uso (serve il Python 3.13 di sistema)") from None
    c = cfg["campo"]
    LAT, LON = c["lat"], c["lon"]
    Z, LATO = 7, 512
    N = 2 ** Z
    mappe = scarica(cfg["fonti"]["rainviewer"], "radar")
    fotogrammi = mappe["radar"]["past"][-7:]  # circa un'ora, un fotogramma ogni 10 minuti
    if not fotogrammi:
        raise FonteNonRisponde("RainViewer non ha fotogrammi")

    def frazione(lat, lon):
        return (lon + 180) / 360 * N, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * N

    def latlon(x, y):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / N)))), x / N * 360 - 180

    def distanza_direzione(lat, lon):
        dy = (lat - LAT) * 111.2
        dx = (lon - LON) * 111.2 * math.cos(math.radians(LAT))
        ang = (math.degrees(math.atan2(dx, dy)) + 360) % 360
        return math.hypot(dx, dy), ["N", "NE", "E", "SE", "S", "SO", "O", "NO"][int((ang + 22.5) // 45) % 8]

    fx, fy = frazione(LAT, LON)
    x0 = int(fx) - (0 if fx % 1 > 0.5 else 1)
    y0 = int(fy) - (0 if fy % 1 > 0.5 else 1)
    cx, cy = (fx - x0) * LATO, (fy - y0) * LATO
    km_px = 40075.017 * math.cos(math.radians(LAT)) / (N * LATO)
    r_px = int(100 / km_px) + 4
    box = (max(int(cx - r_px), 0), max(int(cy - r_px), 0), min(int(cx + r_px), 2 * LATO), min(int(cy + r_px), 2 * LATO))
    esiti, tessere_mancanti = [], 0
    for f in fotogrammi:
        mosaico = Image.new("RGBA", (LATO * 2, LATO * 2))
        for i in range(2):
            for j in range(2):
                url = f"{mappe['host']}{f['path']}/{LATO}/{Z}/{x0 + i}/{y0 + j}/2/0_0.png"
                try:
                    mosaico.paste(Image.open(io.BytesIO(scarica(url, "radar", timeout=30, binario=True))).convert("RGBA"), (i * LATO, j * LATO))
                except (FonteNonRisponde, OSError):
                    tessere_mancanti += 1
        ritaglio = mosaico.crop(box)
        canale = ritaglio.getchannel("A")
        alfa = canale.get_flattened_data() if hasattr(canale, "get_flattened_data") else canale.getdata()
        larg = ritaglio.width
        entro = {10: 0, 25: 0, 50: 0, 100: 0}
        vicina = (None, "-")
        for k, a in enumerate(alfa):
            if a < 30:
                continue
            xx, yy = box[0] + k % larg, box[1] + k // larg
            lat, lon = latlon(x0 + xx / LATO, y0 + yy / LATO)
            d, direz = distanza_direzione(lat, lon)
            for r in entro:
                if d <= r:
                    entro[r] += 1
            if vicina[0] is None or d < vicina[0]:
                vicina = (d, direz)
        esiti.append({"ora": dt.datetime.fromtimestamp(f["time"]).strftime("%H:%M"), "entro": entro,
                      "vicina_km": None if vicina[0] is None else round(vicina[0]), "direzione": vicina[1]})
    if tessere_mancanti == 4 * len(fotogrammi):
        raise FonteNonRisponde("nessuna tessera radar scaricata")
    return {"fotogrammi": esiti, "tessere_mancanti": tessere_mancanti, "km_per_pixel": round(km_px, 2),
            "aggiornato": esiti[-1]["ora"], "ritardo_min": round((adesso.timestamp() - fotogrammi[-1]["time"]) / 60)}


def segnali_radar(cfg, rad):
    s = cfg["soglie"]
    out = []
    if not rad:
        return out
    primo, ultimo = rad["fotogrammi"][0], rad["fotogrammi"][-1]
    vicino = s["radar_raggio_vicino_km"]
    if ultimo["entro"][vicino] > 0 and primo["entro"][vicino] == 0:
        out.append(f"pioggia comparsa entro {vicino} km dal campo nell'ultima ora")
    if ultimo["entro"][100] > primo["entro"][100] and ultimo["entro"][100] >= max(30, s["radar_crescita_x"] * primo["entro"][100]):
        fattore = "da zero" if primo["entro"][100] == 0 else f"×{ultimo['entro'][100] / primo['entro'][100]:.1f}"
        out.append(f"area di pioggia entro 100 km in crescita ({fattore} in un'ora: celle in formazione)")
    return out


def frase_radar(cfg, rad, err):
    if err:
        return f"RADAR: NON DISPONIBILE ({err})."
    s = cfg["soglie"]
    u, p = rad["fotogrammi"][-1], rad["fotogrammi"][0]
    testa = f"RADAR {p['ora']}→{u['ora']}"
    if u["entro"][100] == 0:
        corpo = "nessuna pioggia entro 100 km dal campo"
    else:
        corpo = (f"pioggia entro 10 km: {'sì' if u['entro'][10] else 'no'} · entro 25 km: {'sì' if u['entro'][25] else 'no'} · "
                 f"eco più vicina a {u['vicina_km']} km verso {u['direzione']} · area entro 100 km: {p['entro'][100]}→{u['entro'][100]} pixel (1 px ≈ {str(rad['km_per_pixel']).replace('.', ',')} km)")
    note = ""
    if rad["tessere_mancanti"]:
        note = f" [{rad['tessere_mancanti']} tessere non scaricate]"
    if rad["ritardo_min"] > 25:
        note += f" [ultimo fotogramma di {rad['ritardo_min']} min fa]"
    return f"{testa}: {corpo}.{note} Vale nelle {s['radar_minuti_prima'] // 60} ore prima dell'inizio; con celle in formazione non dice dove pioverà."


# ----------------------------------------------------------------------------- regola
def segnali_temporale(cfg, g, boll, mod, rad, radar_vale, cambio):
    """I segnali che dicono «oggi le celle nascono sul posto»: con uno solo di questi
    il punto non è prevedibile, e lo si scrive nel titolo, non nelle note."""
    s = cfg["soglie"]
    ore = ore_segnali(g["inizio"], g["fine"])
    out = []
    if boll and LIVELLI.get(boll["livelli"]["temporali"], 1) >= 1:
        out.append(f"allerta {boll['livelli']['temporali']} per temporali")
    con_temporale = [m["nome"] for m in mod.values()
                     if any((m["ore"].get(o, {}).get("weather_code") in CODICI_TEMPORALE) for o in ore)]
    if con_temporale:
        out.append("temporali previsti da " + ", ".join(con_temporale))
    cape = [m["ore"].get(o, {}).get("cape") for m in mod.values() for o in ore]
    cape = [c for c in cape if c is not None]
    instabile = f"aria instabile (CAPE fino a {max(cape):.0f} J/kg, soglia {s['cape_instabile']})" if cape and max(cape) >= s["cape_instabile"] else None
    rif = mod.get(cfg["fonti"]["modello_riferimento"])
    if rif:
        lpi = [rif["ore"].get(o, {}).get("lightning_potential") for o in ore]
        lpi = [v for v in lpi if v is not None]
        if lpi and max(lpi) >= s["lpi_fulmini"]:
            out.append(f"potenziale fulmini ICON-2I {max(lpi):.1f} (soglia {s['lpi_fulmini']})")
    if cambio and abs(cambio[0] - cambio[1]) >= s["cambio_idea_mm"]:
        out.append(f"ICON-2I ha cambiato idea rispetto a ieri ({it_mm(cambio[1])} → {it_mm(cambio[0])} mm nella fascia)")
    if radar_vale:
        out.extend(segnali_radar(cfg, rad))
    # Dal 21/10/2026 (Fabrizio): l'aria instabile DA SOLA non è un segnale, è una nota. A Palermo in
    # autunno il CAPE sopra soglia è frequente anche nei giorni asciutti, e un «da confermare» per
    # il solo CAPE insegna a ignorare la pagina. Conta quando arriva insieme a un altro segnale
    # (il 12/10: CAPE 1570 + allerta gialla + ICON-2I che cambia idea).
    if instabile and out:
        out.append(instabile)
    return out, (None if out else instabile)


def verdetto(cfg, g, boll, mod, mod_err, rad, radar_vale, cambio):
    s = cfg["soglie"]
    ore = ore_fascia(g["inizio"], g["fine"])
    n_tot = len(cfg["fonti"]["modelli"])
    mm = {m["nome"]: mm_fascia(m, ore) for m in mod.values()}
    validi = {k: v for k, v in mm.items() if v is not None}
    n_ok = len(validi)
    piovosi = [k for k, v in validi.items() if v >= s["pioggia_mm_modello"]]
    soglia = s["modelli_pioggia_per_annullare"]
    segnali, nota_instabilita = segnali_temporale(cfg, g, boll, mod, rad, radar_vale, cambio)
    righe, titolo = [], ""
    link_boll = cfg["fonti"]["bollettino_pagina"]

    if g["regola"] == "piccoli":
        if boll is None:
            if n_ok >= s["modelli_minimi"] and len(piovosi) >= soglia:
                esito = "ANNULLATO"
                righe.append(f"Pioggia prevista nella fascia da {n_modelli(len(piovosi))} su {n_ok} ({', '.join(piovosi)}); si annulla da {soglia}.")
                righe.append(f"Bollettino allerta non disponibile: controllalo a mano su {link_boll}")
            else:
                esito, titolo = "NON PREVEDIBILE", "bollettino allerta non disponibile"
                righe.append(f"Senza bollettino la regola dei piccoli (annullare con qualsiasi allerta temporali) non si può applicare. Controlla a mano: {link_boll}")
                if n_ok >= s["modelli_minimi"]:
                    righe.append(f"Pioggia prevista da {n_modelli(len(piovosi))} su {n_ok} (si annulla da {soglia}).")
                else:
                    righe.append(f"Hanno risposto {n_modelli(n_ok)} su {n_tot}.")
        elif max(LIVELLI.get(v, 1) for v in boll["livelli"].values()) >= LIVELLI[s.get("piccoli_annullano_da", "GIALLA").upper()]:
            # Dal 21/10/2026 (Fabrizio): i piccoli si annullano per allerta solo da ARANCIONE in su
            # (soglia in config: piccoli_annullano_da). Prima bastava la gialla temporali.
            gravi = [f"{k} {v}" for k, v in boll["livelli"].items() if LIVELLI.get(v, 1) >= LIVELLI[s.get("piccoli_annullano_da", "GIALLA").upper()]]
            esito, titolo = "ANNULLATO", f"allerta {', '.join(gravi)}"
            righe.append(f"Allerta {', '.join(gravi)} sulla zona: i piccoli si annullano con allerta da {s.get('piccoli_annullano_da', 'GIALLA').lower()} in su. Avvisare i genitori.")
        elif LIVELLI.get(boll["livelli"]["temporali"], 1) >= 1 and n_ok >= s["modelli_minimi"] and len(piovosi) >= soglia:
            esito, titolo = "ANNULLATO", "pioggia prevista"
            righe.append(f"Pioggia prevista nella fascia da {n_modelli(len(piovosi))} su {n_ok} ({', '.join(piovosi)}); si annulla da {soglia}. In più allerta {boll['livelli']['temporali']} per temporali.")
        elif LIVELLI.get(boll["livelli"]["temporali"], 1) >= 1:
            esito, titolo = "NON PREVEDIBILE", f"allerta {boll['livelli']['temporali']} temporali"
            righe.append(f"Allerta {boll['livelli']['temporali']} per temporali sulla zona: non si annulla in anticipo, si decide alle 15:30 col bollettino del giorno. Con temporali possibili il punto non è prevedibile.")
            if n_ok:
                righe.append(f"Pioggia prevista da {n_modelli(len(piovosi))} su {n_ok} (si annulla da {soglia}): nei giorni di temporali i modelli non sanno dire dove.")
        elif n_ok < s["modelli_minimi"]:
            esito, titolo = "NON PREVEDIBILE", "dati insufficienti"
            righe.append(f"Hanno risposto solo {n_modelli(n_ok)} su {n_tot} (ne servono almeno {s['modelli_minimi']}): la regola «pioggia prevista da {soglia} su {n_tot}» non si può applicare. Nessuna allerta temporali.")
        elif len(piovosi) >= soglia:
            esito, titolo = "ANNULLATO", "pioggia prevista"
            righe.append(f"Pioggia prevista nella fascia da {n_modelli(len(piovosi))} su {n_ok} ({', '.join(piovosi)}); si annulla da {soglia}. Nessuna allerta temporali.")
        elif segnali and s.get("piccoli_annullano_con_segnali", False):
            esito, titolo = "ANNULLATO", "aria instabile, non prevedibile"
            righe.append("Segnali di temporale: " + "; ".join(segnali) + ".")
            righe.append(f"Per i piccoli «non prevedibile» = si annulla per tempo (scelta in config.toml). Pioggia prevista da {n_modelli(len(piovosi))} su {n_ok}, nessuna allerta.")
        else:
            esito = "SI FA"
            righe.append(f"Nessuna allerta temporali; pioggia prevista da {n_modelli(len(piovosi))} su {n_ok} (si annulla da {soglia}).")
            if segnali:
                titolo = "ATTENZIONE, segnali di temporale"
                righe.append("Segnali di temporale: " + "; ".join(segnali) + ". La regola dice si fa, ma un rovescio isolato sul campo non è prevedibile.")
    else:  # grandi
        livello_max = max(LIVELLI.get(v, 1) for v in boll["livelli"].values()) if boll else 0
        if boll and livello_max >= 2:
            gravi = [f"{k} {v}" for k, v in boll["livelli"].items() if LIVELLI.get(v, 1) >= 2]
            esito, titolo = "ANNULLATO", f"allerta {', '.join(gravi)}"
            righe.append(f"Allerta {', '.join(gravi)} sulla zona: i grandi si annullano con allerta arancione o rossa.")
        elif boll is None:
            esito, titolo = "NON PREVEDIBILE", "bollettino allerta non disponibile"
            righe.append(f"Non so se c'è un'allerta arancione o rossa. Controlla a mano: {link_boll}")
            righe.append("Senza allerta grave i grandi si allenano anche con pioggia. FULMINI VISTI O SENTITI = STOP.")
        elif segnali:
            esito, titolo = "NON PREVEDIBILE", "temporali possibili"
            righe.append("Temporali possibili sul punto: " + "; ".join(segnali) + ".")
            righe.append("Si va al campo sapendo che può diluviare: radar pulito e modelli bassi non valgono quando le celle nascono sul posto. Si annulla solo per fulmini: FULMINI VISTI O SENTITI = STOP.")
        elif n_ok == 0:
            esito, titolo = "SI FA", "modelli non disponibili"
            righe.append("Nessuna allerta arancione/rossa, quindi per regola ci si allena. Ma nessun modello ha risposto: non so se c'è pioggia o aria instabile. Guarda il cielo e la mappa dei fulmini.")
        elif piovosi:
            esito, titolo = "SI FA", "pioggia, ci si allena bagnati"
            righe.append(f"Pioggia prevista da {n_modelli(len(piovosi))} su {n_ok} ({', '.join(piovosi)}) senza segnali di temporale.")
        else:
            esito = "SI FA"
            righe.append(f"Nessuna allerta arancione/rossa, nessun segnale di temporale, nessuno dei {n_ok} modelli prevede pioggia nella fascia.")
            if n_ok < s["modelli_minimi"]:
                righe.append(f"Attenzione: hanno risposto solo {n_modelli(n_ok)} su {n_tot}.")
    if nota_instabilita and esito != "ANNULLATO":
        righe.append(f"Nota: {nota_instabilita}. Da sola non cambia il verdetto: possibili rovesci isolati, non prevedibili.")
    if g["regola"] == "piccoli" and g.get("decisione_entro"):
        righe.append(f"Decisione da comunicare entro le {g['decisione_entro']}.")
    temperatura, abbigliamento = consiglio_abbigliamento(cfg, mod, ore, piovosi)
    if abbigliamento and esito != "ANNULLATO":
        righe.append(f"Abbigliamento: {abbigliamento} ({temperatura:.0f} °C previsti).")
    prob, n_prob = probabilita_pioggia(mod, ore)
    # Con segnali di temporale la probabilità di pioggia dei modelli non vale (le celle nascono sul
    # posto e i modelli non le vedono): il 21/10/2026, allerta gialla e pioggia sul campo al radar, dava
    # 1% e «conferma 99%». Deciso con Fabrizio lo stesso giorno: in quei casi niente percentuale.
    if esito == "ANNULLATO":
        conferma = 0
    elif segnali:
        conferma = None
    else:
        conferma = None if prob is None else 100 - prob
    if segnali and esito != "ANNULLATO":
        righe.append("Conferma non calcolabile: con temporali possibili i modelli non sanno dire dove pioverà.")
    elif segnali:
        righe.append("La probabilità di pioggia dei modelli non vale: con temporali possibili non sanno dire dove pioverà.")
    elif prob is not None:
        righe.append(f"Probabilità di pioggia nella fascia {prob}% (scenari di {n_modelli(n_prob)}): conferma {conferma}%.")
    mm_riga = " · ".join(f"{m['nome']} {it_mm(mm.get(m['nome']))}" for m in cfg["fonti"]["modelli"])
    righe.append(f"mm previsti {ore[0][:2]}-{ore[-1][:2]}: {mm_riga}")
    if cambio and abs(cambio[0] - cambio[1]) < s["cambio_idea_mm"] and (cambio[0] or cambio[1]):
        righe.append(f"ICON-2I ieri per questa fascia dava {it_mm(cambio[1])} mm, oggi {it_mm(cambio[0])}.")
    return {"gruppo": g["nome"], "fascia": f"{g['inizio']}-{g['fine']}", "regola": g["regola"], "esito": esito,
            "titolo": titolo, "motivi": righe, "mm_per_modello": mm, "modelli_piovosi": piovosi, "segnali": segnali,
            "icon2i_oggi_ieri_mm": cambio, "temperatura": temperatura, "abbigliamento": abbigliamento,
            "probabilita_pioggia": prob, "conferma": conferma, "nota_instabilita": nota_instabilita}


def probabilita_pioggia(mod, ore):
    """Probabilità di pioggia (più di 0,1 mm in un'ora) nella fascia, per la «percentuale di
    conferma» chiesta da Fabrizio il 14/10/2026. Non è un'opinione: Open-Meteo la calcola
    sugli scenari (ensemble, ~30 corse) dei modelli che la forniscono — ICON-EU, ECMWF, GFS,
    UKMO; ICON-2I no. Per ogni modello si prende il massimo sulle ore della fascia, poi la
    media fra i modelli. Torna (percentuale intera, quanti modelli) oppure (None, 0).
    La conferma è 100 meno questa probabilità (0 se il verdetto è ANNULLATO): misura la
    pioggia, non i temporali, e in un giorno «non prevedibile» va letta insieme al verdetto."""
    valori = []
    for m in mod.values():
        p = [m["ore"].get(o, {}).get("precipitation_probability") for o in ore]
        p = [v for v in p if v is not None]
        if p:
            valori.append(max(p))
    if not valori:
        return None, 0
    return round(sum(valori) / len(valori)), len(valori)


def consiglio_abbigliamento(cfg, mod, ore, piovosi):
    """Temperatura media prevista nella fascia (media dei modelli che l'hanno data) e il
    consiglio su cosa indossare, con le soglie di [abbigliamento] in config.toml
    (richiesta di Fabrizio del 14/10/2026). Non è una regola del verdetto: è un consiglio
    per atleti e genitori. Torna (temperatura, testo) oppure (None, None)."""
    a = cfg.get("abbigliamento")
    if not a:
        return None, None
    valori = []
    for m in mod.values():
        t = [m["ore"].get(o, {}).get("temperature_2m") for o in ore]
        t = [v for v in t if v is not None]
        if t:
            valori.append(sum(t) / len(t))
    if not valori:
        return None, None
    temperatura = sum(valori) / len(valori)
    if temperatura < a["felpa_sotto"]:
        capo = "felpa"
    elif temperatura < a["maniche_lunghe_sotto"]:
        capo = "maniche lunghe"
    else:
        capo = "maglietta"
    if piovosi and a.get("kway_se_pioggia", True):
        capo += " e k-way per la pioggia"
    return temperatura, capo


# ----------------------------------------------------------------------------- testo
def e_previsione(cfg, data, adesso):
    """Prima del primo controllo del giorno ([controlli].orari[0], oggi 15:30) il verdetto è una
    PREVISIONE: non si annulla e non si conferma niente (Fabrizio, 21/10/2026). Torna l'ora della
    decisione se il verdetto è una previsione, altrimenti None."""
    orari = cfg.get("controlli", {}).get("orari", [])
    if not orari:
        return None
    if data > adesso.date() or (data == adesso.date() and adesso.hour * 60 + adesso.minute < minuti(orari[0])):
        return orari[0]
    return None


def componi(cfg, data, adesso, gruppi, boll, boll_err, mod, mod_err, rad, rad_err, verdetti):
    c = cfg["campo"]
    r = [f"METEO ALLENAMENTO · {GIORNI_LUNGHI[data.weekday()]} {data:%d/%m/%Y} · controllo delle {adesso:%H:%M}",
         f"{c['nome']} · zona allerta: {c['zona_allerta']}"]
    decisione = e_previsione(cfg, data, adesso)
    if gruppi and decisione:
        r.append(f"PREVISIONE: la decisione per tutti i gruppi esce alle {decisione}. Prima di quell'ora nessun allenamento è annullato né confermato.")
    if boll_err:
        r.append(f"ALLERTA: BOLLETTINO NON DISPONIBILE ({boll_err}). Controlla a mano: {cfg['fonti']['bollettino_pagina']}")
    else:
        quando = "oggi" if boll["parte"] == "today" else "domani"
        liv = " · ".join(f"{k} {v}" for k, v in boll["livelli"].items())
        r.append(f"ALLERTA per il {data:%d/%m} (bollettino DPC del {boll['emesso']:%d/%m %H:%M}, parte «{quando}»): {liv}")
        if boll["avviso"]:
            r.append(f"  ! {boll['avviso']}")
    r.append("")
    if not gruppi:
        r.append(f"Oggi ({GIORNI_LUNGHI[data.weekday()]}) nessun gruppo si allena. Per vedere comunque i verdetti: --gruppi tutti")
        r.append("")
    for v in verdetti:
        r.append(f"{v['gruppo'].upper()} · {v['fascia']}")
        r.append(f">> {v['esito']}" + (f" · {v['titolo']}" if v["titolo"] else ""))
        r.extend(v["motivi"])
        r.append("")
    r.append(frase_radar(cfg, rad, rad_err))
    r.append(f"FULMINI: nessuna fonte leggibile in automatico. Prima di uscire e al campo guarda la mappa: {cfg['fonti']['fulmini_mappa']}")
    r.append("  Fulmine visto o sentito = tutti fuori dal campo, qualunque sia il verdetto.")
    fonti = [f"bollettino {'ok' if boll else 'NO'}", f"modelli {len(mod)}/{len(cfg['fonti']['modelli'])}", f"radar {'ok' if rad else 'NO'}"]
    r.append("FONTI: " + " · ".join(fonti))
    if mod_err:
        r.append("  Modelli che NON hanno risposto: " + "; ".join(mod_err.values()) + ".")
    prossimi = [o for o in cfg.get("controlli", {}).get("orari", []) if minuti(o) > adesso.hour * 60 + adesso.minute]
    if gruppi and prossimi:
        r.append(f"Prossimo controllo previsto alle {prossimi[0]}.")
    return "\n".join(r)


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Oggi si fa allenamento o no? (Sharks Palermo)")
    ap.add_argument("--data", help="giorno da valutare, AAAA-MM-GG (default oggi)")
    ap.add_argument("--ora", help="ora del controllo, HH:MM (default adesso; utile per le simulazioni)")
    ap.add_argument("--gruppi", choices=["giorno", "tutti"], default="giorno", help="tutti = anche i gruppi che oggi non si allenano")
    ap.add_argument("--json", action="store_true", help="stampa anche il risultato in JSON")
    ap.add_argument("--simula-guasto", action="append", default=[], choices=["radar", "modelli", "bollettino"], help="fa fallire una fonte di proposito")
    ap.add_argument("--no-salva", action="store_true", help="non scrivere in esiti/")
    ap.add_argument("--config", default=str(QUI / "config.toml"))
    a = ap.parse_args()
    GUASTI.update(a.simula_guasto)

    try:
        cfg = carica_config(a.config)
    except Exception as e:
        print(f"ERRORE in config.toml: {e}")
        return 2
    data = dt.date.fromisoformat(a.data) if a.data else dt.date.today()
    if a.ora:
        adesso = dt.datetime.combine(data, dt.time(*map(int, a.ora.split(":"))))
    elif data == dt.date.today():
        adesso = dt.datetime.now()
    else:
        adesso = dt.datetime.combine(data, dt.datetime.now().time())
    gruppi = gruppi_del_giorno(cfg, data, tutti=(a.gruppi == "tutti"))

    boll, boll_err = None, None
    try:
        boll = bollettino(cfg, data, adesso)
    except FonteNonRisponde as e:
        boll_err = str(e)
    mod, mod_err = previsioni(cfg, data)
    rad, rad_err = None, None
    try:
        rad = radar(cfg, data, adesso)
    except FonteNonRisponde as e:
        rad_err = str(e)
    cambi = {}  # per ogni fascia distinta: (mm oggi, mm ieri) del modello di riferimento
    if cfg["fonti"]["modello_riferimento"] in mod:
        for g in gruppi:
            chiave = tuple(ore_fascia(g["inizio"], g["fine"]))
            if chiave not in cambi:
                try:
                    cambi[chiave] = corsa_precedente(cfg, data, list(chiave))
                except (FonteNonRisponde, KeyError, TypeError):
                    cambi[chiave] = None

    verdetti = []
    for g in gruppi:
        minuti_all_inizio = minuti(g["inizio"]) - (adesso.hour * 60 + adesso.minute)
        radar_vale = rad is not None and data == dt.date.today() and minuti_all_inizio <= cfg["soglie"]["radar_minuti_prima"]
        verdetti.append(verdetto(cfg, g, boll, mod, mod_err, rad, radar_vale, cambi.get(tuple(ore_fascia(g["inizio"], g["fine"])))))

    testo = componi(cfg, data, adesso, gruppi, boll, boll_err, mod, mod_err, rad, rad_err, verdetti)
    risultato = {"data": data.isoformat(), "controllo": adesso.strftime("%Y-%m-%d %H:%M"), "campo": cfg["campo"]["nome"],
                 "bollettino": None if not boll else {**boll, "emesso": boll["emesso"].strftime("%Y-%m-%d %H:%M")},
                 "bollettino_errore": boll_err, "modelli_ok": sorted(m["nome"] for m in mod.values()),
                 "modelli_errore": mod_err, "radar": rad, "radar_errore": rad_err,
                 "verdetti": verdetti, "testo": testo,
                 "decisione_alle": e_previsione(cfg, data, adesso)}
    print(testo)
    if a.json:
        print("\n" + json.dumps(risultato, ensure_ascii=False, indent=1, default=str))
    if not a.no_salva:
        esiti = QUI / "esiti"
        esiti.mkdir(exist_ok=True)
        base = esiti / f"{data.isoformat()}_{adesso:%H%M}"
        base.with_suffix(".txt").write_text(testo, encoding="utf-8")
        base.with_suffix(".json").write_text(json.dumps(risultato, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        (QUI / "ultimo_esito.txt").write_text(testo, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
