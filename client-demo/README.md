# OmniSight client demo

Open `index.html` in a browser to present the verified ShopLite proof of concept.
The page is self-contained except for its local screenshot and works offline.
It clearly labels the evidence as a mock-provider workflow with successful
Docker verification; it does not claim a live Gemini call. Use **Print / save
as PDF** to export a handout.

## Start the interactive application

The repeatable launcher is `run-client-demo.ps1` in this folder. From the
repository root in PowerShell, run:

```powershell
.\client-demo\run-client-demo.ps1
```

It uses the existing `backend/.venv`, reads configuration from the ignored
`backend/.env`, starts PostgreSQL in Docker with a persistent local volume,
applies migrations, and opens the connected OmniSight dashboard. Start a scan
from **Live ShopLite Demo**, review its real backend findings and patch proposal,
then type `approve-demo-only` in the dashboard to run isolated sandbox
verification. Press Enter in the PowerShell window when you are done; the API
and database container stop, while PostgreSQL data remains in its Docker volume.
Docker Desktop must be running. The `.env` must contain a Gemini key and ShopLite
sandbox token; the script never prints credential values. If you shared an API
key in chat, rotate it and put the replacement in `.env` before a client demo.

## Accurate scope

The interactive dashboard displays run data returned by the backend. Gemini
analysis and sandbox verification occur only when you start and approve those
steps. The GitHub App publisher is implemented, but a live draft PR publish has
not been demonstrated; it needs GitHub App credentials. The CI browser test
uses mock AI and verifies capture, proposal review, explicit sandbox approval,
Docker verification, and history after refresh. General-purpose repository
patching and a production dashboard are not part of this milestone.
