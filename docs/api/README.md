# Documentazione API

Riferimento funzionale delle API del backend, organizzato **per area**. Serve al
collaudo manuale, allo sviluppo frontend e a chi riprende lo sviluppo del backend.

## Contenuto

| Area | Sorgente | PDF | Stato |
|:--|:--|:--|:--|
| Booking / Prenotazioni | `booking_api.md` | `BOOKING_API.pdf` | Step A → G |
| Pagamenti | `payment_api.md` | `PAYMENT_API.pdf` | Step G |

Le aree già esistenti ma non ancora documentate (Auth, Users, Rooms, Room Services,
Extra Services) riceveranno un proprio file con la stessa struttura.

## Perché due file per ogni area

Il **Markdown è la fonte di verità**: è diffabile in git, si legge direttamente su
GitHub e le modifiche sono visibili nella cronologia. Il **PDF è un artefatto
derivato**, comodo da leggere, stampare e passare a chi non lavora nel repository.

Modificare il PDF a mano non ha senso: verrebbe sovrascritto alla rigenerazione
successiva. Si modifica il Markdown e si rigenera.

## Rigenerare un PDF

```bash
cd docs/api
python3 build_pdf.py booking_api.md BOOKING_API.pdf
```

Richiede il pacchetto `markdown` e un binario Chromium. Il percorso di Chromium
viene cercato fra quelli elencati in `CHROMIUM_CANDIDATES` dentro `build_pdf.py`:
se sul tuo sistema si trova altrove, aggiungilo lì.

```bash
pip install markdown
```

## Struttura di un documento d'area

Ogni file segue lo stesso indice, così chi ne ha letto uno sa orientarsi negli altri:

1. **Convenzioni generali** — base URL, formati, struttura degli errori, mappa
   eccezione → codice HTTP
2. **Autenticazione e autorizzazione** — meccanismo, livelli di accesso, cosa
   l'area ha introdotto o modificato
3. **Meccanismi trasversali** — rate limiting, captcha, token, e ogni altra
   protezione aggiunta a supporto dell'area
4. **Endpoint** — per ciascuno: accesso, protezioni, schemi, codici di stato,
   logica applicativa, test che lo coprono, esempio `curl`
5. **Schemi** — ogni DTO con campi, tipi, vincoli e obbligatorietà
6. **Enumerazioni** — valori ammessi e significato
7. **Copertura dei test** — quali file coprono cosa
8. **Checklist di verifica manuale** — la sequenza da seguire a collaudo
9. **Changelog** — cosa è cambiato e quando
10. **Da completare** — cosa manca e che impatto avrà sul documento

## Regola di manutenzione

> Il documento dell'area va aggiornato **nello stesso intervento** che modifica gli
> endpoint, gli schemi o le protezioni di quell'area — non in un secondo momento.
> Una documentazione che diverge dal codice è peggio di nessuna documentazione,
> perché induce a fidarsene.

Quando documento e codice non concordano, **fa fede il codice**: la discrepanza è
un errore del documento, da correggere.
