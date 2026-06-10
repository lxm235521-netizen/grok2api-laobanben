# grok2api Remote Release Deployment Notes

Release: `20260609-super-first`

## Scope

This package contains the remote `grok2api` service source used by New API channel 29.

Included behavior:

- Multi-reference image video requests are supported.
- Reference-image video requests prefer `ssoSuper` before `ssoBasic`.
- Text-only video routing keeps the original token pool behavior.
- Reference images are sent through both uploaded asset references and `file_attachments` for the first video round.

## Main Changed Files

- `app/services/grok/services/video.py`
  - Builds reference-image video pool candidates as `ssoSuper -> ssoBasic`.
  - Calls `get_token_for_video(..., respect_pool_candidates_order=True)` when reference images are present.
  - Preserves reference image asset attachment behavior.

- `app/services/token/manager.py`
  - Adds `respect_pool_candidates_order`.
  - Default is `False`, preserving existing behavior for normal video requests.
  - When enabled, token selection follows caller-provided pool order strictly.

## Package Exclusions

The archive intentionally excludes:

- `.env` and `.env.*`
- runtime `data/`
- runtime `logs/`
- Python caches and bytecode
- backup files such as `*.bak_*`
- `.git`

Configure environment variables or `.env` separately on the target host.

## Deploy

From the extracted package directory:

```bash
docker compose build grok2api
docker compose up -d --no-deps grok2api
```

Check service health:

```bash
docker ps --filter name=grok2api
docker logs --tail 120 grok2api-grok2api-1
```

## Verification Performed

After this version was deployed on the remote server, New API channel 29 was tested with:

- `grok-imagine-1.0-video`, `6s`, `480p`, single reference image
- `grok-imagine-1.0-video`, `6s`, `480p`, multiple reference images

Both requests completed successfully with HTTP 200 and `progress=100`.

Remote logs confirmed:

- `pool=ssoSuper`
- reference image counts were prepared correctly
- no Basic-first fallback was used in the final verification

