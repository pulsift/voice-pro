# Voice Pro operations toolkit

All commands read credentials at runtime from the Windows user environment or the canonical migration note. They never persist or print API keys, passwords, HMACs, or JWTs.

Run from `C:\Users\samim\voice-pro` with `backend\.venv\Scripts\python.exe`.

## Safe preflight

```powershell
backend\.venv\Scripts\python.exe scripts\ops\safety_status.py --require-safe
backend\.venv\Scripts\python.exe scripts\ops\voice_railway.py status --expect-sha <commit> --expect-model <model> --expect-reasoning <low-or-none> --wait 600
```

Pass means fulfilment is stubbed, the five real solar campaigns are draft, the kill switch is on, the backend deployment is `SUCCESS`, and its `meta.commitHash` equals the expected commit.

## Prompt

Prompt hashes normalize CRLF to LF and ignore final newlines.

```powershell
backend\.venv\Scripts\python.exe scripts\ops\prompt_sync.py prepare --expected-live-sha <source-sha>
backend\.venv\Scripts\python.exe scripts\ops\prompt_sync.py apply --expected-live-sha <source-sha> --expected-candidate-sha <candidate-sha> --confirm
backend\.venv\Scripts\python.exe scripts\ops\prompt_sync.py verify --expected-sha <candidate-sha>
backend\.venv\Scripts\python.exe scripts\ops\prompt_sync.py restore --expected-live-sha <candidate-sha> --from-file <prompt-before.txt> --backup-sha <source-sha> --confirm
```

`prepare` and `verify` are read-only. `apply` requires both pinned hashes plus `--confirm` and automatically restores the source prompt if PUT verification fails. `restore` requires both the current live hash and backup-file hash. Prompt backups contain no credentials and land under `%TEMP%\voice-pro-ops\<source-hash>`.

## Controlled seeded call

```powershell
backend\.venv\Scripts\python.exe scripts\ops\seeded_call.py --confirm --window 300 --expected-sha <commit> --expected-deployment-id <deployment> --expected-model <model> --expected-reasoning <low-or-none> --expected-prompt-sha <prompt-sha>
```

This can call only the compiled-in Sami seed. Before disarming it requires the exact successful Railway deployment/commit/model, tested prompt hash, backend health, fulfilment stub mode, five draft campaigns, and kill switch ON. It starts a detached watchdog before disarming and re-arms as soon as the first new exact-destination CallRecord appears. It also re-arms in `finally` and keeps the watchdog alive if immediate re-arm cannot be verified.

### Saturday/weekend direct Voice Pro route

VA-10 intentionally enforces its weekday calling window. For a supervised Saturday or weekend test, bypass VA-10 without weakening that production rule:

```powershell
backend\.venv\Scripts\python.exe scripts\ops\seeded_call.py --mode direct --confirm --window 300 --expected-sha <commit> --expected-deployment-id <deployment> --expected-model <model> --expected-reasoning <low-or-none> --expected-prompt-sha <prompt-sha>
```

Direct mode retains the same exact deployment, model, prompt, backend, fulfilment-stub, five-draft-campaign, kill-switch, and stored-seed preflight. It authenticates at runtime and POSTs only the compiled Sami destination, Telnyx caller, agent, and fixed test variables to Voice Pro's existing `/api/v1/telephony/calls` endpoint. It never disarms the kill switch; the switch must remain ON before the POST and throughout CallRecord polling. The default `--mode n8n` behavior is unchanged.

## Evidence and cleanup

```powershell
backend\.venv\Scripts\python.exe scripts\ops\call_pull.py --call-id <call-record-id> --include-transcript
backend\.venv\Scripts\python.exe scripts\ops\call_pull.py --call-id <call-record-id> --assert-booking-sequence
backend\.venv\Scripts\python.exe scripts\ops\cal_booking.py get --uid <booking-uid>
backend\.venv\Scripts\python.exe scripts\ops\cal_booking.py cancel --uid <booking-uid> --reason "Voice Pro controlled test cleanup" --confirm
```

`call_pull.py` masks the destination and allow-lists booking-attempt fields. Cancellation GETs first, POSTs only after explicit confirmation, and GETs again to verify cancelled state.

## Model A/B

```powershell
backend\.venv\Scripts\python.exe scripts\ops\voice_railway.py set-model gpt-realtime-2025-08-28 --reasoning none --confirm --wait 600
backend\.venv\Scripts\python.exe scripts\ops\voice_railway.py set-model gpt-realtime-2.1 --reasoning low --confirm --wait 600
```

Model changes use Railway GraphQL variable mutations, then `deploymentRedeploy` from the latest successful backend deployment. Never use `serviceInstanceDeployV2`.

The model-swap command snapshots existing deployment IDs and passes only after a new deployment ID reaches `SUCCESS` at the exact commit and `/health` reports the expected runtime `realtime_model` and `realtime_reasoning_effort`. After the seeded call, use the `call_probe_started_at` emitted by `seeded_call.py` for the stronger proof that an actual OpenAI session used it:

```powershell
backend\.venv\Scripts\python.exe scripts\ops\voice_railway.py runtime-model --deployment-id <new-deployment> --expect-model <model> --since <call_probe_started_at> --wait 120
```

Pass requires a fresh `connecting_to_openai_realtime` log from that exact deployment naming the expected model.

## Offline checks

```powershell
backend\.venv\Scripts\python.exe -m ruff check scripts\ops
backend\.venv\Scripts\python.exe -m ruff format --check scripts\ops
backend\.venv\Scripts\python.exe -m pytest -q scripts\ops\tests
```
