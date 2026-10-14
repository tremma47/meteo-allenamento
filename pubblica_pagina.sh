#!/usr/bin/env bash
# =====================================================================
#  PUBBLICA LA PAGINA METEO SU www.sharkspalermofootball.com/meteo/
#  Sharks Football ASD - hosting Aruba
#  (copia adattata di Vademecum/pubblica.sh, 20/09/2026)
# ---------------------------------------------------------------------
#  COSA MANCA PERCHE' QUESTO SCRIPT FUNZIONI (non puo' fare da solo):
#  le tre credenziali FTP, che stanno nel pannello Aruba sotto
#  "Gestione Hosting" -> "FTP". Si passano come variabili d'ambiente,
#  NON come argomenti, cosi' la password non finisce nella cronologia
#  e NON si conserva da nessuna parte:
#
#      FTP_HOST=ftp.sharkspalermofootball.com FTP_USER=1648002@aruba.it FTP_PASS=... ./pubblica_pagina.sh
#
#  Questo server NON accetta l'FTP cifrato (provato il 20/09/2026):
#  risponde solo in chiaro sulla porta 21.
# ---------------------------------------------------------------------
#  SICUREZZA: lo script rifiuta per costruzione di scrivere qualunque
#  cosa fuori dalla cartella /meteo/. Non tocca index.php,
#  wp-config.php, wp-content ne' nient'altro del WordPress.
#
#  Senza l'opzione --carica fa solo la ricognizione e non scrive nulla.
#  La pagina si pubblica UNA volta: il verdetto lo legge da GitHub, il
#  sito non si tocca piu' finche' non cambia la pagina stessa.
# =====================================================================

set -uo pipefail

FILE_LOCALE="${FILE_LOCALE:-$(dirname "$0")/pagina/index.html}"
CARTELLA="meteo"
NOME_REMOTO="index.html"
INDIRIZZO="https://www.sharkspalermofootball.com/${CARTELLA}/"

CARICA=0
[ "${1:-}" = "--carica" ] && CARICA=1

for v in FTP_HOST FTP_USER FTP_PASS; do
  if [ -z "${!v:-}" ]; then
    echo "ERRORE: manca la variabile $v. Vedi le istruzioni in testa a questo file."
    exit 1
  fi
done

if [ ! -f "$FILE_LOCALE" ]; then
  echo "ERRORE: non trovo il file da pubblicare: $FILE_LOCALE"
  exit 1
fi

OPZ=(--silent --show-error --connect-timeout 20 --max-time 180)
[ "${FTP_TLS:-0}" = "1" ] && OPZ+=(--ssl-reqd)
CRED=(--user "${FTP_USER}:${FTP_PASS}")

elenca() {  # elenca <percorso-remoto>
  # La barra finale e' obbligatoria: senza, curl tratta l'ultimo pezzo come
  # un FILE da scaricare invece che come una cartella da elencare, e torna
  # vuoto. Un vuoto cosi' si scambia facilmente per "la cartella non esiste".
  local p="$1" url="ftp://${FTP_HOST}/"
  [ -n "$p" ] && url="ftp://${FTP_HOST}/${p}/"
  curl "${OPZ[@]}" "${CRED[@]}" --list-only "$url" 2>/dev/null
}

echo "=== 1. Connessione a ${FTP_HOST} come ${FTP_USER} ==="
RADICE="$(elenca "")"
if [ -z "$RADICE" ]; then
  echo "ERRORE: connessione riuscita ma cartella vuota, oppure credenziali rifiutate."
  echo "        NOTA (provato il 20/09/2026): questo server NON accetta FTP_TLS=1,"
  echo "        risponde solo in chiaro sulla porta 21. Se hai messo FTP_TLS=1, toglilo."
  exit 1
fi
echo "$RADICE" | sed 's/^/    /'

echo
echo "=== 2. Individuo la document root (la cartella con wp-config.php) ==="
DOCROOT=""
TROVATA=0
# L'ordine conta. Su questo hosting la document root NON e' la radice FTP:
# e' la cartella "www.sharkspalermofootball.com". Accanto a lei, nella radice,
# Aruba tiene "..._Backup_Giornaliero" e "..._Backup_Settimanale", che sono
# copie complete del sito, wp-config.php incluso: se lo script le accettasse
# come document root pubblicherebbe dentro un backup invece che sul sito.
# Per questo il salto esplicito qui sotto, e per questo la lista e' fissa
# invece di essere una scansione automatica di tutte le cartelle.
for c in "www.sharkspalermofootball.com" "sharkspalermofootball.com" "" "www" "htdocs" "httpdocs" "public_html" "web"; do
  case "$c" in *[Bb]ackup*) continue ;; esac
  CONTENUTO="$(elenca "$c")"
  if echo "$CONTENUTO" | grep -qx "wp-config.php" && echo "$CONTENUTO" | grep -qx "wp-content"; then
    DOCROOT="$c"
    TROVATA=1
    echo "    TROVATA in: /${c:-<radice FTP>}"
    break
  fi
done

if [ "$TROVATA" -eq 0 ]; then
  echo "    NON TROVATA. Nessuna delle cartelle provate contiene sia wp-config.php sia wp-content."
  echo "    Fermo qui senza scrivere niente: la cartella va individuata a mano."
  exit 1
fi

PREFISSO="${DOCROOT:+$DOCROOT/}"
DESTINAZIONE="${PREFISSO}${CARTELLA}/${NOME_REMOTO}"

# --- rete di sicurezza: la destinazione deve stare dentro /meteo/ ---
case "$DESTINAZIONE" in
  *"/${CARTELLA}/${NOME_REMOTO}"|"${CARTELLA}/${NOME_REMOTO}") : ;;
  *) echo "BLOCCO DI SICUREZZA: destinazione sospetta ($DESTINAZIONE). Non scrivo."; exit 1 ;;
esac

echo
echo "=== 3. Controllo che la cartella /${CARTELLA}/ non esista gia' ==="
ESISTENTE="$(elenca "${PREFISSO}${CARTELLA}")"
if [ -n "$ESISTENTE" ]; then
  echo "    La cartella esiste gia' e contiene:"
  echo "$ESISTENTE" | sed 's/^/        /'
  echo "    (normale se stai aggiornando la pagina: index.html verra' sostituito,"
  echo "     il resto del sito non viene toccato)"
else
  echo "    Non esiste: verra' creata."
fi

echo
echo "=== 4. Riepilogo ==="
echo "    File locale ..... $FILE_LOCALE ($(wc -c < "$FILE_LOCALE") byte)"
echo "    Destinazione .... /${DESTINAZIONE}"
echo "    Indirizzo ....... $INDIRIZZO"

if [ "$CARICA" -eq 0 ]; then
  echo
  echo ">>> RICOGNIZIONE SOLTANTO: non ho scritto nulla."
  echo ">>> Per caricare davvero, rilancia lo stesso comando aggiungendo  --carica"
  exit 0
fi

echo
echo "=== 5. Carico il file ==="
if curl "${OPZ[@]}" "${CRED[@]}" --ftp-create-dirs -T "$FILE_LOCALE" "ftp://${FTP_HOST}/${DESTINAZIONE}"; then
  echo "    Caricamento completato."
else
  echo "    ERRORE durante il caricamento."
  exit 1
fi

echo
echo "=== 6. Verifica dal web ==="
for u in "$INDIRIZZO" "${INDIRIZZO%/}"; do
  STATO="$(curl -sS -o /dev/null -w '%{http_code}' -L "$u")"
  echo "    $u  ->  HTTP $STATO"
done
TITOLO="$(curl -sS -L "$INDIRIZZO" | grep -oE '<title>[^<]*</title>' | head -1)"
echo "    Titolo servito: ${TITOLO:-(nessuno)}"
echo
echo "FATTO. Apri $INDIRIZZO per il controllo a vista (anche dal telefono)."
