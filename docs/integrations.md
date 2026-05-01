# Optional integrations

## Linked-app status

The header strip can show a one-line summary of a local helper app — for
example, an LLM server reporting which models it has loaded. To enable it,
have your app write a small JSON file once per second to:

```
~/.cassie/status.json
```

(The path is currently hardcoded — see `minomon/data/sampler.py::_CASSIE_PATH`
if you want to point it elsewhere. PRs welcome to make this configurable.)

### Schema

```json
{
  "fast_loaded": true,
  "deep_loaded": false,
  "fast_resident_gb": 14.2,
  "deep_resident_gb": 0.0,
  "in_flight": false,
  "tts_in_flight": false,
  "last_request_unix": 1730412345,
  "updated_unix": 1730412350
}
```

| Field | Meaning |
|---|---|
| `fast_loaded` / `deep_loaded` | Whether each model is currently loaded in memory. Only `fast_loaded` is required. |
| `fast_resident_gb` / `deep_resident_gb` | Approximate resident size. Just for display. |
| `in_flight` | True while a generation is mid-flight. Lights up "generating…" in the UI. |
| `tts_in_flight` | Reserved for TTS state. |
| `last_request_unix` | Unix timestamp of last request. Used to compute idle duration. |
| `updated_unix` | When this file was written. If older than 10s, integration is treated as offline. |

If the file doesn't exist or is stale, the status row is hidden — no error,
no clutter.

### Why "Cassie"?

Mino Monitor was originally built for a personal assistant project named
Cassie. The integration name stuck. Treat it as a generic "linked app"
contract — anything that produces this JSON shape will light up the row.
