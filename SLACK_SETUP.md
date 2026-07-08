# SLACK_SETUP.md — connecting agentdesk to a Slack workspace (M8)

Manual steps to create the Slack app and wire it to the local stack. Everything here is
workspace-admin clicking; no code changes. Design rationale (Socket Mode vs Events API,
identity mapping, guardrails) lives in ADR-038/039/041.

The integration runs **Socket Mode**: the runner (`make slack`) opens an outbound WebSocket
to Slack, so no public URL, tunnel, or Request-URL verification is needed — the whole flow
works from a laptop. See ADR-038 for when a deployed installation should switch to the
Events API instead.

## 1. Create the app from the manifest

1. Go to <https://api.slack.com/apps> → **Create New App** → **From a manifest**.
2. Pick your workspace, paste the manifest below (JSON tab), create.

```json
{
    "display_information": {
        "name": "agentdesk",
        "description": "AI service desk: file or dedup-link IT tickets from Slack threads",
        "background_color": "#1f2430"
    },
    "features": {
        "bot_user": {
            "display_name": "agentdesk",
            "always_online": true
        }
    },
    "oauth_config": {
        "scopes": {
            "bot": [
                "app_mentions:read",
                "channels:history",
                "groups:history",
                "reactions:read",
                "chat:write",
                "users:read",
                "users:read.email"
            ]
        }
    },
    "settings": {
        "event_subscriptions": {
            "bot_events": [
                "app_mention",
                "reaction_added"
            ]
        },
        "interactivity": {
            "is_enabled": false
        },
        "org_deploy_enabled": false,
        "socket_mode_enabled": true,
        "token_rotation_enabled": false
    }
}
```

Scope map (least privilege — why each one exists):

| Scope | Used by |
|---|---|
| `app_mentions:read` | the @mention trigger event |
| `reactions:read` | the `:ticket:` reaction trigger event |
| `channels:history`, `groups:history` | `conversations.replies` — pulling the triggering thread (public + private channels the bot is in) |
| `chat:write` | posting the in-thread reply (`post_slack_message` + the runner's identity-fallback message) |
| `users:read`, `users:read.email` | `users.info` — mapping the triggering Slack user's profile **email** to a seeded service-desk user (ADR-039; both scopes are required for the email field to appear) |

## 2. Tokens → `.env`

1. **App-level token** (Socket Mode): app page → **Basic Information → App-Level Tokens** →
   **Generate Token and Scopes** → name it `socket`, add scope `connections:write` →
   copy the `xapp-…` token → `.env` `SLACK_APP_TOKEN=xapp-…`
2. **Bot token**: **Install App** → **Install to Workspace** → allow → copy the
   **Bot User OAuth Token** (`xoxb-…`) → `.env` `SLACK_BOT_TOKEN=xoxb-…`

Both empty = Slack is off: the runner refuses to start, `post_slack_message` no-ops with a
logged error dict, CI and evals never need a workspace (recorded fixtures — ADR-039).

## 3. Identity prerequisite (the demo trick)

Tickets are filed as the **seeded user whose email matches the triggering Slack profile**
(fail-closed: no match → the bot replies that it can't file automatically, and nothing runs).
The 50 seeded users have `@corp.com` emails, so for a live demo either:

- set your Slack profile email to `demo.user@corp.com` (profile → Edit → Email; workspace
  admin can edit member emails), or
- re-seed one user's email to your real Slack email:
  ```sql
  UPDATE users SET email = 'you@your-workspace-domain.com'
  WHERE email = 'demo.user@corp.com';
  ```
  (revert afterwards — the eval suites act as `demo.user@corp.com`.)

## 4. Run it

```bash
make db-up          # postgres + redis (as usual)
make api            # the chat API on :8000 — the runner is a pure client of it
make slack          # the Socket Mode runner (separate terminal)
```

Then in Slack:

1. **Invite the bot** to a channel: `/invite @agentdesk`.
2. In any thread (or as a new message): **@agentdesk** `please file this`, **or** react to
   any message of the thread with the trigger emoji **:ticket:** 🎫
   (`SLACK_TRIGGER_EMOJI` changes it).
3. The bot pulls the whole thread, runs the normal pipeline (router → incident agent:
   summarize → dedup → create or link), and replies **in the thread** with the ticket id and
   one suggested KB article. Re-triggering the same thread continues the same conversation
   (deterministic session id — ADR-039).

## Troubleshooting

- **Runner exits immediately** — one of the two tokens is missing/blank in `.env`.
- **No reaction to @mention** — bot not invited to the channel, or the runner terminal shows
  the event but `CHAT_API_URL` doesn't point at a running API.
- **"couldn't match your Slack account"** reply — the triggering user's Slack profile email
  doesn't equal any `users.email` row (step 3). This is fail-closed by design; it never
  guesses an identity.
- **Reply missing but ticket created** — check the runner/API logs for
  `post_slack_message`: a missing `chat:write` scope or the bot not being in the channel
  makes Slack reject the post (the agent's turn still completes; the error dict is logged).
- **Duplicate processing after laptop sleep** — Socket Mode redelivers unacked envelopes;
  the runner dedupes by `event_id` in memory, but a restarted runner may reprocess the last
  trigger. Re-triggers land in the same session, so the agent sees its earlier decision.
