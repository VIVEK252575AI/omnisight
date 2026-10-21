# OmniSight client demo

Open `index.html` in a browser to present the verified ShopLite proof of concept.
The page is self-contained except for its local screenshot and works offline.
Use **Print / save as PDF** to export a handout.

## Re-run the live demo

The repeatable launcher is `run-client-demo.ps1` in this folder. From the
repository root in PowerShell, run:

```powershell
.\client-demo\run-client-demo.ps1
```

It uses the existing `backend/.venv`, reads the Gemini key and sandbox approval
token from the ignored `backend/.env`, starts the API and ShopLite on loopback,
runs capture → Gemini detection → safe patch proposal → Docker verification,
then stops both local servers. On success it opens this client demo page. Docker
Desktop must be running and the local `.env` must contain the required Gemini,
ShopLite allow-list, and sandbox settings. The script never prints credential
values. Rotate any API key shared in chat and put the replacement in `.env`
before a client presentation.

## Accurate scope

The evidence shown on the page is from a real Gemini request and a successful
ShopLite sandbox run. The GitHub App publisher is implemented, but a live draft
PR publish has not been demonstrated; that needs local PostgreSQL and GitHub App
credentials. General-purpose repository patching and a production dashboard
are not part of this proof of concept.
