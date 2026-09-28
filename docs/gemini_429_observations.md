# Gemini API 429 Rate Limit Observations

Empirically captured by firing requests in a tight loop against free-tier
models until each limit was hit. Tested models:

| Model | RPM | TPM | RPD |
|---|---|---|---|
| `gemini-3.8-flash` | 5 | 250K | 20 |
| `gemini-3-flash-preview` | 5 | 250K | 20 |

## Key finding: RPM and RPD are the same HTTP shape

Both limit types return:
- HTTP `429`
- `error.status`: `"RESOURCE_EXHAUSTED"`
- A `google.rpc.QuotaFailure` detail with a `violations[].quotaId` field
- A `google.rpc.RetryInfo` detail with a `retryDelay` in seconds

**The only reliable distinguisher is `quotaId`:**

| Limit | `quotaId` contains | `retryDelay` range observed |
|---|---|---|
| RPM | `PerMinute` | ~50s (remainder of current minute) |
| RPD | `PerDay` | ~18–40s (remainder of current minute — **not hours**) |

**Critical:** RPD `retryDelay` is NOT hours-long. It is the same order of
magnitude as RPM (~seconds). A delay-threshold heuristic cannot distinguish
them. Only `quotaId` is reliable.

**503 UNAVAILABLE** is a separate transient overload error with no quota
context — observed frequently on `gemini-3-flash-preview` (5 out of 21
requests before RPD was hit). It carries no `RetryInfo` and should be retried
on the same model with short exponential backoff, independent of quota state.

---

## Sample: RPM limit hit (`GenerateRequestsPerMinutePerProjectPerModel-FreeTier`)

Triggered after 5 successful requests within one minute.

```json
{
  "error": {
    "code": 429,
    "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. \n* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 5, model: gemini-3.8-flash\nPlease retry in 50.209367667s.",
    "status": "RESOURCE_EXHAUSTED",
    "details": [
      {
        "@type": "type.googleapis.com/google.rpc.Help",
        "links": [
          {
            "description": "Learn more about Gemini API quotas",
            "url": "https://ai.google.dev/gemini-api/docs/rate-limits"
          }
        ]
      },
      {
        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
        "violations": [
          {
            "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
            "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
            "quotaDimensions": {
              "location": "global",
              "model": "gemini-3.8-flash"
            },
            "quotaValue": "5"
          }
        ]
      },
      {
        "@type": "type.googleapis.com/google.rpc.RetryInfo",
        "retryDelay": "50s"
      }
    ]
  }
}
```

---

## Sample: RPD limit hit (`GenerateRequestsPerDayPerProjectPerModel-FreeTier`)

Triggered after 20 total requests across multiple minute windows.

```json
{
  "error": {
    "code": 429,
    "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. \n* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.8-flash\nPlease retry in 39.883911039s.",
    "status": "RESOURCE_EXHAUSTED",
    "details": [
      {
        "@type": "type.googleapis.com/google.rpc.Help",
        "links": [
          {
            "description": "Learn more about Gemini API quotas",
            "url": "https://ai.google.dev/gemini-api/docs/rate-limits"
          }
        ]
      },
      {
        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
        "violations": [
          {
            "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
            "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
            "quotaDimensions": {
              "location": "global",
              "model": "gemini-3.8-flash"
            },
            "quotaValue": "20"
          }
        ]
      },
      {
        "@type": "type.googleapis.com/google.rpc.RetryInfo",
        "retryDelay": "39s"
      }
    ]
  }
}
```

Note: subsequent RPD 429s in the same minute window showed `retryDelay`
counting down (30s, 25s, 24s, 19s, 18s...) — it reflects the remainder of
the current minute, not the time until the daily quota resets.

Confirmed identical response shape on `gemini-3-flash-preview` — RPD hit
after exactly 20 successful requests, `quotaDimensions.model` reports
`gemini-3-flash` (the canonical name behind the preview alias),
`retryDelay: 30s`.

---

## Sample: 503 transient overload

Observed intermittently with no retry hint. Notably frequent on
`gemini-3-flash-preview` (5 out of 21 requests in a single run). Carries no
`QuotaFailure` or `RetryInfo` — unrelated to quota state.

```json
{
  "error": {
    "code": 503,
    "message": "This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.",
    "status": "UNAVAILABLE"
  }
}
```

---

## Implications for `src/agent/core/gemini.py`

| Condition | Detection | Action |
|---|---|---|
| RPM hit | `quotaId` contains `PerMinute` | Sleep `retryDelay`, retry same model once |
| RPD hit | `quotaId` contains `PerDay` | Fall back to next model in chain |
| 5xx / transport error | HTTP 5xx or status 0 | Retry same model up to 3× with exponential backoff (1s, 2s, 4s), then fall back |
| All models exhausted | End of chain | Exponential backoff cooldown, retry whole chain up to `GEMINI_MAX_RETRIES` |

The `retryDelay` from `RetryInfo` is used as the RPM sleep duration, and also
as a hint for the chain-retry cooldown (if larger than the exponential backoff
value). The `Retry-After` HTTP response header is also checked and the larger
of the two values wins.
