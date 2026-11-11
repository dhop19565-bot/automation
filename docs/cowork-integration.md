# Connecting CoworkProvider to a Copilot Studio workflow

`CoworkProvider` is a placeholder. It calls nothing at Microsoft, and this repository
contains no Cowork or Copilot endpoint, URL or request shape. This note explains what
has to be built to turn it on, and why the frontend does not change when that happens.

## Why the frontend does not change

The browser never talks to a provider. It talks to four hub endpoints:

| Frontend needs | Endpoint | What a new provider changes |
| --- | --- | --- |
| Which providers exist, whether each is available, and how to label it | `GET /api/providers` | The Cowork entry flips from `available: false, execution_mode: "not_connected"` to `available: true, execution_mode: "real_ai"`. The card becomes selectable and its badge changes, because both are rendered from this response. |
| Start a run | `POST /api/runs` with `"provider": "cowork"` | Nothing. The provider id is just a value. |
| Follow a run | `GET /api/runs/{id}` (polled) | Nothing. Same statuses, same proposal shape, same validation report. |
| Approve, reject | `POST /api/runs/{id}/approve` etc. | Nothing. |

Behind the API, the engine depends only on the provider contract in
`hub/providers/base.py`:

```python
ProviderTask(run_id, workflow, instructions, payload, output_schema, options)
    -> ProviderResult(output, execution_mode, model, provider_request_id, usage, notes)
```

Validation, approval, posting, audit and the checklist all run on `ProviderResult.output`
and are identical for every provider. `tests/test_engine.py::test_cowork_works_end_to_end_once_a_transport_is_supplied`
plugs a fake transport into `CoworkProvider` and takes a run to `awaiting_approval`
with no other code changed.

## What has to be built

One class and one line of wiring.

1. **Implement `CoworkTransport`** (defined in `hub/providers/cowork.py`) in a new
   module, for example `hub/integrations/copilot_studio.py`:

   ```python
   class CopilotStudioTransport:
       label = "Copilot Studio agent <name>"

       async def invoke(self, task: ProviderTask) -> dict:
           # 1. open a conversation with the published agent
           # 2. send task.instructions, task.payload and task.output_schema
           # 3. wait for the agent's reply
           # 4. return it as a JSON object
   ```

2. **Wire it in `build_providers`** (`hub/main.py`) when its configuration is present:

   ```python
   CoworkProvider(CopilotStudioTransport.from_env() if copilot_configured() else None)
   ```

Credentials for it live in server environment variables, exactly as the Anthropic key
does. Nothing reaches the browser.

## Supported ways to reach a Copilot Studio agent

These two routes came from a search of Microsoft Learn on 11 November 2026. The pages
themselves could not be opened from the build environment, so the wording below is from
search summaries. Read both pages before building; this area changes quickly.

- **Microsoft 365 Agents SDK, Copilot Studio client.** An external application connects
  to a published agent using a connection string, or the environment ID, tenant ID and
  agent schema name from the agent's settings. For user sign-in, the app registration
  needs the Power Platform API delegated permission `CopilotStudio.Copilots.Invoke`.
  Start at: [Integrate with web or native apps using Microsoft 365 Agents SDK](https://learn.microsoft.com/en-us/microsoft-copilot-studio/publication-integrate-web-or-native-app-m365-agents-sdk).
- **Direct Line.** Described as the fallback when the Agents SDK does not cover a
  scenario, with service principal tokens given as one such case. The client gets its
  token from the agent's own token endpoint.
  Start at: [Publish an agent to mobile or custom apps](https://learn.microsoft.com/en-us/microsoft-copilot-studio/publication-connect-bot-to-custom-application).

Not verified here, so treat as leads rather than facts:

- Whether an agent flow or Power Automate flow with an HTTP trigger is a supported way
  to start this kind of work from an external server.
- Whether Copilot Cowork itself exposes any supported programmatic entry point. Nothing
  in this repository assumes it does. The route above goes through a Copilot Studio
  agent that you publish and control.

## Decisions to make before building

1. **Whose identity runs the agent?** The hub is a server. The documented SDK route is
   built around a signed-in user, and the search summary says it does not support
   service principal tokens. Either the hub obtains a delegated token for the person who
   started the run, or you use Direct Line. This is the main open question and it is a
   Microsoft-side design choice, not a hub one.
2. **Structured output.** `ClaudeProvider` has the API constrain the answer to the JSON
   Schema. A Copilot Studio agent gives no such guarantee, so its instructions must tell
   it to return JSON matching `task.output_schema`. The hub already handles the failure
   cases: an answer that does not fit the schema fails the run with
   `PROVIDER_OUTPUT_INVALID`, and one that fits but is wrong is stopped by validation.
3. **Duration.** `invoke` may take as long as it needs up to
   `HUB_PROVIDER_TIMEOUT_SECONDS`; the run shows as *Running* and the UI keeps polling.
   If the agent replies by callback rather than in the conversation, the transport
   waits for that callback inside `invoke`. The engine does not change either way.
4. **Data.** Today only fictional data is sent. Routing real ledger data through a
   Copilot Studio agent needs its own review of tenant data-loss-prevention policy and
   where the agent's model runs.

## What stays true after Cowork is connected

- A Cowork proposal gets the same independent recalculation as any other. It cannot be
  approved if a blocking check fails.
- Maker-checker, proposal-hash binding, duplicate prevention and the audit log apply
  unchanged, because they sit in the engine, below the provider.
- D365 posting is still simulated until `LedgerPostingClient` is given a real
  implementation. That is a separate integration from this one.
