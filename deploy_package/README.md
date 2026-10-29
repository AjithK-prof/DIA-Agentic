# DIA Agentic - how to run

## Run on your own machine (Windows)

1. Install Python 3.12.
2. Open PowerShell in this folder and install the packages:
   ```
   py -m pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in the values you were given.
4. Start the app:
   ```
   py -m uvicorn dia_agentic.app:app --port 8007
   ```
5. Open http://localhost:8007 and log in with the user and password from `.env`.
6. Upload an invoice CSV or Excel file to start a run.

The app starts with an empty database. It is created automatically in
`dia_agentic/data/` on first start.

## Deploy on Render (free plan)

1. Push this folder to a GitHub repository.
2. In Render: New -> Blueprint -> pick the repository. It reads `render.yaml`.
3. Under Environment, set `CLOUDFLARE_AUTH_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`,
   `TYPESAFE_API_KEY` and a strong `DIA_DEFAULT_PASSWORD`.

On the free plan, all uploads, runs and decisions are lost when the service
restarts or sleeps (after about 15 minutes idle). Use small sample files.

## Run the tests (optional)

```
py -m unittest discover -s tests -p "test_*.py"
```
