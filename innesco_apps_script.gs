/*
 * ============================================================================
 *  SHARKS - INNESCO DEL METEO ALLENAMENTO (Google Apps Script)
 *  Versione del 21/10/2026. Gira sui server di Google nell'account
 *  sharkspalermofootball@gmail.com, quindi anche col PC spento.
 * ============================================================================
 *
 *  COSA FA: ogni 10 minuti guarda l'ora italiana e, nelle finestre giuste, chiede a
 *  GitHub di far partire il controllo meteo (workflow "meteo.yml" del deposito).
 *  Cosa fare lassù (controllo completo, rilettura del calendario Golee, niente) lo
 *  decide fotografa.py: qui non c'è nessuna regola meteo.
 *    - dalle 15:30 alle 21:59: un innesco ogni 10 minuti (controlli delle 15:30 e
 *      17:30 + rilettura del calendario Golee);
 *    - dalle 23:00 alle 23:19: verdetto orientativo per l'indomani.
 *  Perché esiste: il cron di GitHub è partito 23 volte in una settimana invece di ~380,
 *  e il 19/10/2026 ha saltato i controlli delle 15:30 e 17:30.
 *
 *  DA FARE A MANO (il codice non può farlo da solo):
 *    1. creare il progetto su script.google.com con l'account Sharks, NON con TLT;
 *    2. in Impostazioni progetto -> Proprietà script aggiungere:
 *         GITHUB_TOKEN = la chiave GitHub (fine-grained, solo «Actions: Read and write»
 *                        sul deposito meteo-allenamento). È un segreto: sta SOLO qui,
 *                        mai nel deposito, mai in chat.
 *         REPO         = tremma47/meteo-allenamento   (oppure sharks-palermo/meteo-allenamento
 *                        se il deposito passa all'organizzazione: in quel caso serve anche
 *                        una chiave nuova, intestata all'organizzazione)
 *    3. lanciare UNA volta setupInnesco() dal menu accanto a "Esegui" e accettare i permessi;
 *    4. sul trigger creato impostare "Notifiche errori: immediatamente": se GitHub
 *       rifiuta la chiamata (chiave scaduta: durano al massimo un anno) arriva un'email.
 *
 *  FUNZIONI DA LANCIARE:
 *    setupInnesco()   crea il trigger ogni 10 minuti (cancella quelli vecchi) e fa una prova
 *    provaInnesco()   chiede a GitHub una corsa adesso (innocua: fuori orario non fa niente)
 *    innesco()        quella che lancia il trigger
 */

var FUSO = 'Europe/Rome';
var WORKFLOW = 'meteo.yml';
var FINESTRE = [            // [inizio, fine) in minuti dalla mezzanotte, ora italiana
  [15 * 60 + 30, 22 * 60],  // 15:30 - 21:59
  [23 * 60, 23 * 60 + 20]   // 23:00 - 23:19
];

function minutiItaliani() {
  var hhmm = Utilities.formatDate(new Date(), FUSO, 'HH:mm').split(':');
  return Number(hhmm[0]) * 60 + Number(hhmm[1]);
}

function inFinestra(m) {
  return FINESTRE.some(function (f) { return m >= f[0] && m < f[1]; });
}

/** Chiede a GitHub una corsa del workflow. Se GitHub non risponde 204, lancia un errore:
 *  è l'errore che fa partire l'email di notifica del trigger. */
function avviaWorkflow() {
  var p = PropertiesService.getScriptProperties();
  var token = p.getProperty('GITHUB_TOKEN');
  var repo = p.getProperty('REPO') || 'tremma47/meteo-allenamento';
  if (!token) throw new Error('Manca GITHUB_TOKEN nelle Proprietà script (Impostazioni progetto).');
  var url = 'https://api.github.com/repos/' + repo + '/actions/workflows/' + WORKFLOW + '/dispatches';
  var r = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify({ref: 'main', inputs: {forza: 'false'}}),
    headers: {
      'Authorization': 'Bearer ' + token,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28'
    },
    muteHttpExceptions: true
  });
  var codice = r.getResponseCode();
  if (codice === 204) return 'GitHub ha accettato la richiesta (' + repo + ').';
  var motivo = codice === 401 ? 'chiave scaduta o non valida: va rifatta (istruzioni in MeteoAllenamento/CONTESTO.md)'
             : codice === 404 ? 'deposito o workflow non trovato, oppure la chiave non vede questo deposito (controllare REPO)'
             : codice === 403 ? 'la chiave non ha il permesso «Actions: Read and write» su questo deposito'
             : 'risposta inattesa';
  throw new Error('Innesco meteo: GitHub ha risposto HTTP ' + codice + ' - ' + motivo + '. ' +
                  r.getContentText().slice(0, 200));
}

/** La lancia il trigger ogni 10 minuti. Fuori dalle finestre non fa niente. */
function innesco() {
  if (!inFinestra(minutiItaliani())) return;
  console.log(avviaWorkflow());
}

function provaInnesco() {
  console.log('Ora italiana: ' + Utilities.formatDate(new Date(), FUSO, 'dd/MM/yyyy HH:mm') +
              (inFinestra(minutiItaliani()) ? ' (dentro una finestra)' : ' (fuori dalle finestre: lassù non farà niente)'));
  console.log(avviaWorkflow());
}

function setupInnesco() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'innesco') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('innesco').timeBased().everyMinutes(10).create();
  console.log('Trigger creato: innesco() ogni 10 minuti.');
  provaInnesco();
  console.log('SETUP COMPLETATO. Ora, nel pannello Trigger (icona sveglia), apri il trigger e metti «Notifiche errori: immediatamente».');
}
