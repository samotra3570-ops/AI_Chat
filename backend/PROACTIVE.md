# Explicitly approved character proactive generation (D052)

`/health` advertises `features.proactive_generation: 1`. Existing reply payloads omit `purpose` and retain their behavior. A proactive request uses `purpose: "proactive"`, empty `text`, no image and no `regenerate_of`; bounded history must contain a real nonblank user text turn. Group generation remains unsupported. An invalid purpose or malformed proactive payload fails before reserving budget or calling a provider.

The authenticated quote endpoint returns `purpose: "proactive"` plus the usual model and reserved micro-USD bound. The app rejects a missing capability echo. The user must approve that quote in the app. Generation uses the same request ID, full payload digest, approved bound, per-request/day/month server hard caps, and conservative uncertain-cost reservation. Result lookup never initiates another provider generation. No retry, provider account secret, ledger migration, model/pricing change or server cap increase is introduced.

For OpenAI Responses, the current bounded canonical conversation is followed by a developer control instruction to initiate a brief natural character message. For Claude Messages, a clearly labeled runtime-control user-role turn follows the actual conversation because that API uses alternating messages. The control is not a user event or chat bubble in the app. Neither instruction claims that a new user message was sent. Only the resulting character bubbles enter canonical conversation history.

The client requires an explicitly enabled local contact policy, saved unexpired contact proposal, real delivered user text, eligibility checks, and quote approval. It records the attempt before submission; uncertain completion is recovered with the same request ID. Before paid submission it rechecks current policy, quiet hours, schedule, blocked/hidden state, unread/new pending user content, model and context/history. Cancellation is available before submission; an already started or uncertain generation can only be looked up. The local contact attempt quota is not refunded on refusal, save failure or cancellation after approval. Server money caps remain authoritative and independent from this local frequency limit.

This release does not add automatic background execution, scheduled paid calls, push notifications, group generation, new credentials, or provider credit-balance scraping. No real paid provider call is required for host tests.

Provider primary references:
- https://developers.openai.com/api/reference/resources/responses/methods/create
- https://platform.claude.com/docs/en/api/messages/create
