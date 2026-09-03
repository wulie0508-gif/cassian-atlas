# Doubao provider configuration

## Private settings

The setup script writes `%USERPROFILE%\.opentutor\doubao.env`. This file is outside the teaching repository and must never be committed, attached to a lesson, pasted into chat, or uploaded to Feishu.

Required settings:

```text
OPEN_TUTOR_DOUBAO_API_KEY=<private key>
OPEN_TUTOR_DOUBAO_BASE_URL=https://ark.cn-beijing.volces.com/api/v3/chat/completions
OPEN_TUTOR_DOUBAO_MODEL=<Ark endpoint or model ID>
```

Optional settings:

```text
OPEN_TUTOR_DOUBAO_TIMEOUT_SECONDS=30
OPEN_TUTOR_DOUBAO_MAX_RETRIES=2
OPEN_TUTOR_DOUBAO_RETRY_BASE_SECONDS=0.5
```

Environment variables with the same names override the private file for the current process. The configuration status command reports only whether a key exists; it never prints the key.

## First-time setup

1. In Volcano Engine Ark, create or select a vision-capable inference endpoint.
2. Copy its endpoint/model ID.
3. Run `scripts/configure_doubao.ps1` locally.
4. Paste the API key only into the hidden terminal prompt and enter the model ID when asked.
5. Run `python scripts/doubao_extract.py status`.
6. Test with one non-sensitive sample page before processing learner evidence.

## Privacy and token controls

- One page is one provider request; item coordinates are included in a compact manifest.
- Cache identity is derived from image hash, model, prompt version, and sanitized manifest.
- A cache stores candidate results, not credentials or image bytes.
- Answer keys, learner identities, scores, diagnoses, private paths, and peer-provider outputs are rejected from outbound manifests.
- The provider transcribes visible answers only. It does not grade or correct them.
